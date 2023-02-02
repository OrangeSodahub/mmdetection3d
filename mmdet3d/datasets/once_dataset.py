# Copyright (c) OpenMMLab. All rights reserved.
import copy
import os
import tempfile
from os import path as osp

import mmcv
import numpy as np
import torch
from mmcv.utils import print_log

from ..core import show_multi_modality_result, show_result
from ..core.bbox import (Box3DMode, CameraInstance3DBoxes, Coord3DMode,
                         LiDARInstance3DBoxes, points_cam2img)
from .builder import DATASETS
from .custom_3d import Custom3DDataset
from .pipelines import Compose


@DATASETS.register_module()
class OnceDataset(Custom3DDataset):
    r"""ONCE Dataset.

    This class serves as the API for experiments on the `ONCE Dataset
    <https://once-for-auto-driving.github.io/download>`_.

    Args:
        data_root (str): Path of dataset root.
        ann_file (str): Path of annotation file.
        split (str): Split of input data.
        pts_prefix (str, optional): Prefix of points files.
            Defaults to 'velodyne'.
        pipeline (list[dict], optional): Pipeline used for data processing.
            Defaults to None.
        classes (tuple[str], optional): Classes used in the dataset.
            Defaults to None.
        modality (dict, optional): Modality to specify the sensor data used
            as input. Defaults to None.
        box_type_3d (str, optional): Type of 3D box of this dataset.
            Based on the `box_type_3d`, the dataset will encapsulate the box
            to its original format then converted them to `box_type_3d`.
            Defaults to 'LiDAR' in this dataset. Available options includes

            - 'LiDAR': Box in LiDAR coordinates.
            - 'Depth': Box in depth coordinates, usually for indoor dataset.
            - 'Camera': Box in camera coordinates.
        filter_empty_gt (bool, optional): Whether to filter empty GT.
            Defaults to True.
        test_mode (bool, optional): Whether the dataset is in test mode.
            Defaults to False.
        pcd_limit_range (list, optional): The range of point cloud used to
            filter invalid predicted boxes.
            Default: [0, -40, -3, 70.4, 40, 0.0].
    """
    CLASSES = ('Car', 'Bus', 'Truck', 'Pedestrain', 'Cyclist')

    def __init__(self,
                 data_root,
                 ann_file,
                 split,
                 pts_prefix='velodyne',
                 pipeline=None,
                 classes=None,
                 modality=None,
                 box_type_3d='LiDAR',
                 filter_empty_gt=True,
                 test_mode=False,
                 pcd_limit_range=[0, -40, -3, 70.4, 40, 0.0],
                 **kwargs):
        super().__init__(
            data_root=data_root,
            ann_file=ann_file,
            pipeline=pipeline,
            classes=classes,
            modality=modality,
            box_type_3d=box_type_3d,
            filter_empty_gt=filter_empty_gt,
            test_mode=test_mode,
            **kwargs)

        self.split = split
        self.root_split = os.path.join(self.data_root, split)
        assert self.modality is not None
        self.pcd_limit_range = pcd_limit_range
        self.pts_prefix = pts_prefix

        self.camera_list = ['cam01', 'cam03', 'cam05', 'cam06', 'cam07', 'cam08', 'cam09']
        self.filtered_data_infos = list(filter(self._check_annos, self.data_infos))
    
    def _check_annos(self, info):
        return 'annos' in info

    def get_data_info(self, index):
        """Get data info according to the given index.

        Args:
            index (int): Index of the sample data to get.

        Returns:
            dict: Data information that will be passed to the data
                preprocessing pipelines. It includes the following keys:

                - sample_idx (str): Sample index.
                - pts_filename (str): Filename of point clouds.
                - img_prefix (str): Prefix of image files.
                - img_filename (str, optional): image filename.
                - lidar2img (list[np.ndarray], optional): Transformations
                    from lidar to different cameras.
                - ann_info (dict): Annotation info.
        """
        info = self.data_infos[index]
        sample_idx = info['frame_id']
        pts_filename = info['lidar_path']
        
        img_filenames = []
        lidar2imgs = []
        seq_id = info['sequence_id']
        for camera in self.camera_list:
            img_filename = os.path.join(self.data_root, 'data', \
                                        seq_id, camera, f'{sample_idx}.jpg')
            img_filenames.append(img_filename)
            # obtain lidar to image transformation matrix
            cam2lidar = info['calib'][camera]['cam_to_velo']
            lidar2cam = np.linalg.inv(cam2lidar)
            intrinsic = info['calib'][camera]['cam_intrinsic']
            viewpad = np.eye(4)
            viewpad[:3, :3] = intrinsic
            lidar2img = viewpad @ lidar2cam.T
            lidar2imgs.append(lidar2img)

        input_dict = dict(
            sample_idx=sample_idx,
            pts_filename=pts_filename,
            img_prefix=None,
            img_filename=img_filenames,
            lidar2img=lidar2imgs,
        )

        if not self.test_mode:
            annos = self.get_ann_info(index)
            input_dict['ann_info'] = annos

        return input_dict

    def get_ann_info(self, index):
        """Get annotation info according to the given index.

        Args:
            index (int): Index of the annotation data to get.

        Returns:
            dict: annotation information consists of the following keys:

                - gt_bboxes_3d (:obj:`LiDARInstance3DBoxes`):
                    3D ground truth bboxes.
                - gt_labels_3d (np.ndarray): Labels of ground truths.
                - gt_bboxes (np.ndarray): 2D ground truth bboxes.
                - gt_labels (np.ndarray): Labels of ground truths.
                - gt_names (list[str]): Class names of ground truths.
        """
        # Use index to get the annos, thus the evalhook could also use this api
        info = self.data_infos[index]
        if 'annos' not in info:
            return None
        annos = info['annos']

        gt_bboxes_3d = annos['boxes_3d']
        # Convert gt_bboxes_3d from once's lidar coordinates
        # to LiDARInstance3DBoxes standard lidar coordinates
        # once (lidar): x(left), y(back), z(up)
        # standard (kitti's lidar): x(front), y(left), z(up)
        # and once `(cx, cy, cz)` is the center of the cubic
        Tr_lidar_to_standard = np.array(
            [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
        )
        gt_bboxes_3d[:, :3] = np.array([Tr_lidar_to_standard @ gt_bbox_3d for \
                                gt_bbox_3d in gt_bboxes_3d[:, :3]])
        gt_bboxes_3d[:, 6] += np.pi / 2
        gt_bboxes_3d = LiDARInstance3DBoxes(
            gt_bboxes_3d,
            box_dim=gt_bboxes_3d.shape[-1],
            origin=(0.5, 0.5, 0.5)).convert_to(self.box_mode_3d)

        gt_names = annos['name']
        gt_labels = []
        for cat in gt_names:
            if cat in self.CLASSES:
                gt_labels.append(self.CLASSES.index(cat))
            else:
                gt_labels.append(-1)
        gt_labels = np.array(gt_labels).astype(np.int64)
        gt_labels_3d = copy.deepcopy(gt_labels)

        anns_results = dict(
            gt_bboxes_3d=gt_bboxes_3d,
            gt_labels_3d=gt_labels_3d,
            gt_labels=gt_labels,
            gt_names=gt_names,
        )
        return anns_results

    def prepare_train_data(self, index):
        """Training data preparation.

        Args:
            index (int): Index for accessing the target data.

        Returns:
            dict: Training data dict of the corresponding index.
        """
        input_dict = self.get_data_info(index)
        # TODO: Need to check
        if input_dict is None or input_dict['ann_info'] is None:
            return None
        self.pre_pipeline(input_dict)
        example = self.pipeline(input_dict)
        if self.filter_empty_gt and \
                (example is None or
                    ~(example['gt_labels_3d']._data != -1).any()):
            return None
        return example

    def _format_results(self, results, jsonfile_prefix=None):
        """Convert the results to the standard format.

        Args:
            results (list[dict]): Testing results of the dataset.
                - boxes_3d (:obj:`LiDARInstance3DBoxes`): Detection bbox.
                - scores_3d (torch.Tensor): Detection scores.
                - labels_3d (torch.Tensor): Predicted box labels.
            jsonfile_prefix (str): The prefix of the output jsonfile.
                You can specify the output directory/filename by
                modifying the jsonfile_prefix. Default: None.

        Returns:
            str: Path of the output json file.
        """

        annos = []
        for idx, result in enumerate(
                mmcv.track_iter_progress(results)):
            info = self.data_infos[idx]
            sample_idx = info['frame_id']
            pred_scores = result['scores_3d'].numpy()
            pred_labels = result['labels_3d'].numpy()
            pred_boxes = self._format_boxes_3d(result['boxes_3d'])

            num_samples = pred_scores.shape[0]
            pred_dict = {
                'name': np.zeros(num_samples),
                'score': np.zeros(num_samples),
                'boxes_3d': np.zeros((num_samples, 7))
            }
            if num_samples != 0:
                pred_dict['name'] = np.array(self.CLASSES)[pred_labels - 1]
                pred_dict['score'] = pred_scores
                pred_dict['boxes_3d'] = pred_boxes

            pred_dict['frame_id'] = sample_idx
            annos.append(pred_dict)

        if jsonfile_prefix is not None:
            mmcv.mkdir_or_exist(jsonfile_prefix)
            res_path = osp.join(jsonfile_prefix, 'results_once.json')
            print('Results writes to', res_path)
            mmcv.dump(annos, res_path)
        return res_path

    def _format_boxes_3d(self, boxes_3d):
        """Format predicted boxes3d to once format

        Args:
            boxes_3d: (:obj:`BaseInstance3DBoxes`): Detection bbox.

        Returns:
            np.ndarray: List of once boxes
        """
        boxes_3d = boxes_3d.tensor.numpy()
        # x,y,z from LiDARInstance3DBoxes to once
        # bottom center to gravity center
        # transform the yaw angle
        Tr_standard_to_lidar = np.array(
            [[0, 1, 0], [-1, 0, 0], [0, 0, 1]]
        )
        boxes_3d[:, :3] = np.array([Tr_standard_to_lidar @ box_3d for \
                                    box_3d in boxes_3d[:, :3]])
        boxes_3d[:, 2] = boxes_3d[:, 2] + boxes_3d[:, 5] * 0.5
        boxes_3d[:, 6] -= np.pi / 2

        return boxes_3d

    def format_results(self,
                       results,
                       jsonfile_prefix=None,
                       submission_prefix=None):
        """Format the results to json file.

        Args:
            results (list[dict]): Testing results of the dataset.
            jsonfile_prefix (str): The prefix of json files. It includes
                the file path and the prefix of filename, e.g., "a/b/prefix".
                If not specified, a temp file will be created. Default: None.
            submission_prefix (str): The prefix of submitted files. It
                includes the file path and the prefix of filename, e.g.,
                "a/b/prefix". If not specified, a temp file will be created.
                Default: None.

        Returns:
            tuple: (result_files, tmp_dir), result_files is a dict containing
                the json filepaths, tmp_dir is the temporal directory created
                for saving json files when jsonfile_prefix is not specified.
        """
        assert isinstance(results, list), 'results must be a list'
        assert len(results) == len(self), (
            'The length of results is not equal to the dataset len: {} != {}'.
            format(len(results), len(self)))
        
        if jsonfile_prefix is None:
            tmp_dir = tempfile.TemporaryDirectory()
            jsonfile_prefix = osp.join(tmp_dir.name, 'results')
        else:
            tmp_dir = None

        if not ('pts_bbox' in results[0] or 'img_bbox' in results[0]):
            result_files = self._format_results(results, jsonfile_prefix)
        else:
            # should take the inner dict out of 'pts_bbox' or 'img_bbox' dict
            result_files = dict()
            for name in results[0]:
                print(f'\nFormating bboxes of {name}')
                results_ = [out[name] for out in results]
                tmp_file_ = osp.join(jsonfile_prefix, name)
                result_files.update(
                    {name: self._format_results(results_, tmp_file_)})
        return result_files, tmp_dir


    def evaluate(self,
                 results,
                 metric='bbox',
                 logger=None,
                 jsonfile_prefix=None,
                 show=False,
                 out_dir=None,
                 pipeline=None):
        """Evaluation in ONCE protocol.

        Args:
            results (list[dict]): Testing results of the dataset.
            metric (str | list[str], optional): Metrics to be evaluated.
                Default: None.
            logger (logging.Logger | str, optional): Logger used for printing
                related information during evaluation. Default: None.
            jsonfile_prefix (str, optional): The prefix of json files, including
                the file path and the prefix of filename, e.g., "a/b/prefix".
                If not specified, a temp file will be created. Default: None.
            submission_prefix (str, optional): The prefix of submission data.
                If not specified, the submission data will not be generated.
                Default: None.
            out_dir (str, optional): Path to save the visualization results.
                Default: None.
            pipeline (list[dict], optional): raw data loading for showing.
                Default: None.

        Returns:
            dict[str, float]: Results of each evaluation metric.
        """
        result_files, tmp_dir = self.format_results(results, jsonfile_prefix)
        from mmdet3d.core.evaluation import once_eval
        gt_annos = [info['annos'] for info in self.data_infos]

        if isinstance(result_files, dict):
            ap_dict = dict()
            for name, results_files_ in result_files.items():
                eval_types = ['Overall&Distance']
                ap_result_str, ap_dict_ = once_eval(
                    gt_annos,
                    results_files_,
                    self.CLASSES,
                    eval_types=eval_types)
                for ap_type, ap in ap_dict_.items():
                    ap_dict[f'{name}/{ap_type}'] = float('{:.4f}'.format(ap))

                print_log(
                    f'Results of {name}:\n' + ap_result_str, logger=logger)
        else:
            ap_result_str, ap_dict = once_eval(gt_annos, result_files, self.CLASSES)
            print_log('\n' + ap_result_str, logger=logger)
        
        if tmp_dir is not None:
            tmp_dir.cleanup()

        return ap_dict