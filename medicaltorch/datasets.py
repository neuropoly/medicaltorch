import os
import re
import collections

from medicaltorch import transforms as mt_transforms

from tqdm import tqdm
import numpy as np
import nibabel as nib

from torch.utils.data import Dataset
import torch
from torch._six import string_classes, int_classes

from PIL import Image

__numpy_type_map = {
    'float64': torch.DoubleTensor,
    'float32': torch.FloatTensor,
    'float16': torch.HalfTensor,
    'int64': torch.LongTensor,
    'int32': torch.IntTensor,
    'int16': torch.ShortTensor,
    'int8': torch.CharTensor,
    'uint8': torch.ByteTensor,
}


class SampleMetadata(object):
    def __init__(self, d=None):
        self.metadata = {} or d

    def __setitem__(self, key, value):
        self.metadata[key] = value

    def __getitem__(self, key):
        return self.metadata[key]

    def __contains__(self, key):
        return key in self.metadata

    def keys(self):
        return self.metadata.keys()


class BatchSplit(object):
    def __init__(self, batch):
        self.batch = batch

    def __iter__(self):
        batch_len = len(self.batch["input"])
        for i in range(batch_len):
            single_sample = {k: v[i] for k, v in self.batch.items()}
            single_sample['index'] = i
            yield single_sample
        raise StopIteration


class SegmentationPair2D(object):
    """This class is used to build 2D segmentation datasets. It represents
    a pair of of two data volumes (the input data and the ground truth data).

    :param input_filenames: the input filename list (supported by nibabel). For single channel, the list will contain 1
                           input filename.
    :param gt_filename: the ground-truth filename.
    :param metadata: metadata list with each item corresponding to an image (modality) in input_filenames.  For single channel, the list will contain metadata related to
                     to one image.
    :param cache: if the data should be cached in memory or not.
    :param canonical: canonical reordering of the volume axes.
    """

    def __init__(self, input_filenames, gt_filenames, metadata=None, cache=True, canonical=False):

        self.input_filenames = input_filenames
        self.gt_filenames = gt_filenames
        self.metadata = metadata
        self.canonical = canonical
        self.cache = cache

        # list of the images
        self.input_handle = []

        # loop over the filenames (list)
        for input_file in self.input_filenames:
            input_img = nib.load(input_file)
            self.input_handle.append(input_img)
            if len(input_img.shape) > 3:
                raise RuntimeError("4-dimensional volumes not supported.")

        # list of GT for multiclass segmentation
        self.gt_handle = []

        # Unlabeled data (inference time)
        if self.gt_filenames is not None:
            for gt in self.gt_filenames:
                if gt is not None:
                    self.gt_handle.append(nib.load(gt))
                else:
                    self.gt_handle.append(None)

        # Sanity check for dimensions, should be the same
        input_shape, gt_shape = self.get_pair_shapes()

        if self.gt_filenames is not None:
            if not np.allclose(input_shape, gt_shape):
                raise RuntimeError('Input and ground truth with different dimensions.')

        if self.canonical:
            for idx, handle in enumerate(self.input_handle):
                self.input_handle[idx] = nib.as_closest_canonical(handle)

            # Unlabeled data
            if self.gt_filenames is not None:
                for idx, gt in enumerate(self.gt_handle):
                    if gt is not None:
                        self.gt_handle[idx] = nib.as_closest_canonical(gt)

        if self.metadata:
            self.metadata = []
            for data, input_filename in zip(metadata, input_filenames):
                data["input_filenames"] = input_filename
                data["gt_filenames"] = gt_filenames
                self.metadata.append(data)

    def get_pair_shapes(self):
        """Return the tuple (input, ground truth) representing both the input
        and ground truth shapes."""
        input_shape = []
        for handle in self.input_handle:
            input_shape.append(handle.header.get_data_shape())

            if not len(set(input_shape)):
                raise RuntimeError('Inputs have different dimensions.')

        gt_shape = []
            
        for gt in self.gt_handle:
            if gt is not None:
                gt_shape.append(gt.header.get_data_shape())

                if not len(set(gt_shape)):
                    raise RuntimeError('Labels have different dimensions.')

        return input_shape[0], gt_shape[0] if len(gt_shape) else None

    def get_pair_data(self):
        """Return the tuble (input, ground truth) with the data content in
        numpy array."""
        cache_mode = 'fill' if self.cache else 'unchanged'

        input_data = []
        for handle in self.input_handle:
            input_data.append(handle.get_fdata(cache_mode, dtype=np.float32))

        gt_data = []
        # Handle unlabeled data
        if self.gt_handle is None:
            gt_data = None
        for gt in self.gt_handle:
            if gt is not None:
                gt_data.append(gt.get_fdata(cache_mode, dtype=np.float32))
            else:
                gt_data.append(np.zeros(self.input_handle[0].shape, dtype=np.float32))

        return input_data, gt_data

    def get_pair_slice(self, slice_index, slice_axis=2):
        """Return the specified slice from (input, ground truth).

        :param slice_index: the slice number.
        :param slice_axis: axis to make the slicing.
        """
        if self.cache:
            input_dataobj, gt_dataobj = self.get_pair_data()
        else:
            # use dataobj to avoid caching
            input_dataobj = [handle.dataobj for handle in self.input_handle]

            if self.gt_handle is None:
                gt_dataobj = None
            else:
                gt_dataobj = [gt.dataobj for gt in self.gt_handle]

        if slice_axis not in [0, 1, 2]:
            raise RuntimeError("Invalid axis, must be between 0 and 2.")

        input_slices = []
        # Loop over modalities
        for data_object in input_dataobj:
            if slice_axis == 2:
                input_slices.append(np.asarray(data_object[..., slice_index],
                                              dtype=np.float32))
            elif slice_axis == 1:
                input_slices.append(np.asarray(data_object[:, slice_index, ...],
                                              dtype=np.float32))
            elif slice_axis == 0:
                input_slices.append(np.asarray(data_object[slice_index, ...],
                                              dtype=np.float32))

        # Handle the case for unlabeled data
        gt_meta_dict = None
        if self.gt_handle is None:
            gt_slices = None
        else:
            gt_slices = []
            for gt_obj in gt_dataobj:
                if slice_axis == 2:
                    gt_slices.append(np.asarray(gt_obj[..., slice_index],
                                          dtype=np.float32))
                elif slice_axis == 1:
                    gt_slices.append(np.asarray(gt_obj[:, slice_index, ...],
                                          dtype=np.float32))
                elif slice_axis == 0:
                    gt_slices.append(np.asarray(gt_obj[slice_index, ...],
                                          dtype=np.float32))

            gt_meta_dict = []
            for gt in self.gt_handle:
                if gt is not None:
                    gt_meta_dict.append(SampleMetadata({
                        "zooms": gt.header.get_zooms()[:2],
                        "data_shape": gt.header.get_data_shape()[:2],
                        "gt_filenames": self.metadata[0]["gt_filenames"]
                    }))
                else:
                    gt_meta_dict.append(SampleMetadata({
                    }))

        input_meta_dict = []
        for handle in self.input_handle:
            input_meta_dict.append(SampleMetadata({
                "zooms": handle.header.get_zooms()[:2],
                "data_shape": handle.header.get_data_shape()[:2],
            }))

        dreturn = {
            "input": input_slices,
            "gt": gt_slices,
            "input_metadata": input_meta_dict,
            "gt_metadata": gt_meta_dict,
        }

        if self.metadata:
            for idx, metadata in enumerate(self.metadata):  # loop across channels
                metadata["slice_index"] = slice_index
                self.metadata[idx] = metadata
                for metadata_key in metadata.keys():  # loop across input metadata
                    dreturn["input_metadata"][idx][metadata_key] = metadata[metadata_key]

        return dreturn


class MRI2DSegmentationDataset(Dataset):
    """This is a generic class for 2D (slice-wise) segmentation datasets.

    :param filename_pairs: a list of tuples in the format (input filename list containing all modalities,
                           ground truth filename, ROI filename, metadata).
    :param slice_axis: axis to make the slicing (default axial).
    :param cache: if the data should be cached in memory or not.
    :param transform: transformations to apply.
    """

    def __init__(self, filename_pairs, slice_axis=2, cache=True,
                 transform=None, slice_filter_fn=None, canonical=False):

        self.indexes = []
        self.filename_pairs = filename_pairs
        self.transform = transform
        self.cache = cache
        self.slice_axis = slice_axis
        self.slice_filter_fn = slice_filter_fn
        self.canonical = canonical
        self.n_contrasts = len(self.filename_pairs[0][0])

        self._load_filenames()

    def _load_filenames(self):
        for input_filenames, gt_filenames, roi_filename, metadata in self.filename_pairs:
            roi_pair = SegmentationPair2D(input_filenames, roi_filename, metadata=metadata,
                                          cache=self.cache, canonical=self.canonical)

            seg_pair = SegmentationPair2D(input_filenames, gt_filenames, metadata=metadata,
                                          cache=self.cache, canonical=self.canonical)

            input_data_shape, _ = seg_pair.get_pair_shapes()

            for idx_pair_slice in range(input_data_shape[self.slice_axis]):
                slice_seg_pair = seg_pair.get_pair_slice(idx_pair_slice,
                                                         self.slice_axis)
                if self.slice_filter_fn:
                    filter_fn_ret_seg = self.slice_filter_fn(slice_seg_pair)
                if self.slice_filter_fn and not filter_fn_ret_seg:
                    continue

                slice_roi_pair = roi_pair.get_pair_slice(idx_pair_slice,
                                                         self.slice_axis)

                item = (slice_seg_pair, slice_roi_pair)
                self.indexes.append(item)

    def set_transform(self, transform):
        """ This method will replace the current transformation for the
        dataset.

        :param transform: the new transformation
        """
        self.transform = transform

    def compute_mean_std(self, verbose=False):
        """Compute the mean and standard deviation of the entire dataset per modality.

        :param verbose: if True, it will show a progress bar.
        :returns: tuple (mean, std dev)
        """
        sum_intensities = np.array([0.0] * self.n_contrasts)
        numel = np.array([0] * self.n_contrasts)

        with DatasetManager(self, override_transform=mt_transforms.ToTensor()) as dset:
            pbar = tqdm(dset, desc="Mean calculation", disable=not verbose)
            for sample in pbar:
                for i in range(self.n_contrasts):
                    input_data = sample['input'][i]
                    sum_intensities[i] += input_data.sum()
                    numel[i] += input_data.numel()
                pbar.set_postfix(means=("-{:.2f} -" * self.n_contrasts).format(*(sum_intensities / numel)),
                                 refresh=False)

            training_mean = sum_intensities / numel
            sum_var = np.array([0.0] * self.n_contrasts)
            numel = np.array([0] * self.n_contrasts)

            pbar = tqdm(dset, desc="Std Dev calculation", disable=not verbose)
            for sample in pbar:
                for i in range(self.n_contrasts):
                    input_data = sample['input'][i]
                    sum_var[i] += (input_data - training_mean[i]).pow(2).sum()
                    numel[i] += input_data.numel()
                pbar.set_postfix(stds=("-{:.2f} -" * self.n_contrasts).format(*np.sqrt(sum_var / numel)),
                                 refresh=False)

            training_std = np.sqrt(sum_var / numel)
        # Converting tensors to numpy array
        training_mean = [training_mean[i].item() for i in range(self.n_contrasts)]
        training_std = [training_std[i].item() for i in range(self.n_contrasts)]
        return training_mean, training_std

    def __len__(self):
        """Return the dataset size."""
        return len(self.indexes)

    def __getitem__(self, index):
        """Return the specific index (input, ground truth, roi and metadatas).

        :param index: slice index.
        """
        seg_pair_slice, roi_pair_slice = self.indexes[index]

        input_tensors = []
        input_metadata = []
        data_dict = {}

        # Looping over all modalities (one or more)
        for idx, input_slice in enumerate(seg_pair_slice["input"]):
            # Consistency with torchvision, returning PIL Image
            # Using the "Float mode" of PIL, the only mode
            # supporting unbounded float32 values

            input_img = Image.fromarray(input_slice, mode='F')
            input_tensors.append(input_img)

        gt_img = []
        for gt_slice in seg_pair_slice["gt"]:
            # Handle unlabeled data
            if gt_slice is None:
                gt_img.append(None)
            else:
                gt_scaled = (gt_slice * 255).astype(np.uint8)
                gt_img.append(Image.fromarray(gt_scaled, mode='L'))

        if not len(roi_pair_slice['gt']):
            roi_img = None
            roi_pair_slice['gt_metadata'] = None
        else:
            roi_img = []
            
        for roi_slice in roi_pair_slice["gt"]:
            # Handle data with no ROI provided
            if roi_pair_slice["gt"] is None:
                roi_img.append(None)
            else:
                roi_scaled = (roi_slice * 255).astype(np.uint8)
                roi_img.append(Image.fromarray(roi_scaled, mode='L'))

        data_dict = {
            'input': input_tensors,
            'gt': gt_img,
            'roi': roi_img,
            'input_metadata': seg_pair_slice['input_metadata'],
            'gt_metadata': seg_pair_slice['gt_metadata'],
            'roi_metadata': roi_pair_slice['gt_metadata']
        }

        """"
        Moving that part in ToTensor() transformation
        input_tensors.append(data_dict['input'])
        input_metadata.append(data_dict['input_metadata'])
        
        if len(input_tensors) > 1:
            data_dict['input'] = torch.squeeze(torch.stack(input_tensors, dim=0))
            data_dict['input_metadata'] = input_metadata
        """
        # Warning: both input_tensors and input_metadata are list. Transforms needs to take that into account.

        if self.transform is not None:
            data_dict = self.transform(data_dict)

        return data_dict


class MRI3DSegmentationDataset(Dataset):
    """This is a generic class for 3D segmentation datasets.
    :param filename_pairs: a list of tuples in the format (input filename,
                           ground truth filename).
    :param cache: if the data should be cached in memory or not.
    :param transform: transformations to apply.
    """

    def __init__(self, filename_pairs, cache=True,
                 transform=None, canonical=False):
        self.filename_pairs = filename_pairs
        self.handlers = []
        self.indexes = []
        self.transform = transform
        self.cache = cache
        self.canonical = canonical

        self._load_filenames()

    def _load_filenames(self):
        for input_filename, gt_filename, roi_filename, metadata in self.filename_pairs:
            segpair = SegmentationPair2D(input_filename, gt_filename, metadata=metadata,
                                         cache=self.cache, canonical=self.canonical)
            self.handlers.append(segpair)

    def set_transform(self, transform):
        """This method will replace the current transformation for the
        dataset.
        :param transform: the new transformation
        """
        self.transform = transform

    def __len__(self):
        """Return the dataset size."""
        return len(self.handlers)

    def __getitem__(self, index):
        """Return the specific index pair volume (input, ground truth).
        :param index: volume index.
        """
        input_img, gt_img = self.handlers[index].get_pair_data()
        data_dict = {
            'input': input_img,
            'gt': gt_img
        }
        if self.transform is not None:
            data_dict = self.transform(data_dict)
        return data_dict


class MRI3DSubVolumeSegmentationDataset(MRI3DSegmentationDataset):
    """This is a generic class for 3D segmentation datasets. This class overload
    MRI3DSegmentationDataset by splitting the initials volumes in several
    subvolumes. Each subvolumes will be of the sizes of the length parameter.

    This class also implement a padding parameter, which overlap the borders of
    the different (the borders of the upper-volume aren't superposed). For
    example if you have a length of (32,32,32) and a padding of 16, your final
    subvolumes will have a total lengths of (64,64,64) with the voxels contained
    outside the core volume and which are shared with the other subvolumes.

    Be careful, the input's dimensions should be compatible with the given
    lengths and paddings. This class doesn't handle missing dimensions.

    :param filename_pairs: a list of tuples in the format (input filename,
                           ground truth filename).
    :param cache: if the data should be cached in memory or not.
    :param transform: transformations to apply.
    :param length: size of each dimensions of the subvolumes
    :param padding: size of the overlapping per subvolume and dimensions
    """

    def __init__(self, filename_pairs, cache=True,
                 transform=None, canonical=False, length=(64, 64, 64), padding=0):
        super().__init__(filename_pairs, cache, transform, canonical)
        self.length = length
        self.padding = padding
        self.transform = transform
        self._prepare_indexes()

    def _prepare_indexes(self):
        length = self.length
        padding = self.padding

        crop = False
        for transfo in self.transform.transforms:
            if "CenterCrop3D" in str(type(transfo)):
                crop = True
                shape_crop = transfo.size
                break

        for i in range(0, len(self.handlers)):
            if not crop:
                input_img, _ = self.handlers[i].get_pair_data()
                shape = input_img[0].shape
            else:
                shape = shape_crop
            if (shape[0] - 2 * padding) % length[0] != 0 or shape[0] % 16 != 0\
                    or (shape[1] - 2 * padding) % length[1] != 0 or shape[1] % 16 != 0 \
                    or (shape[2] - 2 * padding) % length[2] != 0 or shape[2] % 16 != 0:
                raise RuntimeError('Input shape of each dimension should be a \
                                    multiple of length plus 2 * padding and a multiple of 16.')

            for x in range(length[0] + padding, shape[0] - padding + 1, length[0]):
                for y in range(length[1] + padding, shape[1] - padding + 1, length[1]):
                    for z in range(length[2] + padding, shape[2] - padding + 1, length[2]):
                        self.indexes.append({
                            'x_min': x - length[0] - padding,
                            'x_max': x + padding,
                            'y_min': y - length[1] - padding,
                            'y_max': y + padding,
                            'z_min': z - length[2] - padding,
                            'z_max': z + padding,
                            'handler_index': i})

    def __len__(self):
        """Return the dataset size. The number of subvolumes."""
        return len(self.indexes)

    def __getitem__(self, index):
        """Return the specific index pair subvolume (input, ground truth).

        :param index: subvolume index.
        """
        coord = self.indexes[index]
        input_img, gt_img = self.handlers[coord['handler_index']].get_pair_data()
        data_shape = gt_img[0].shape
        seg_pair_slice = self.handlers[coord['handler_index']].get_pair_slice(coord['handler_index'])
        data_dict = {
            'input': input_img,
            'gt': gt_img
        }

        for idx in range(len(data_dict['input'])):
            data_dict['input'][idx] = data_dict['input'][idx][coord['x_min']:coord['x_max'],
                                      coord['y_min']:coord['y_max'],
                                      coord['z_min']:coord['z_max']]

        for idx in range(len(data_dict['gt'])):
            data_dict['gt'][idx] = data_dict['gt'][idx][coord['x_min']:coord['x_max'],
                              coord['y_min']:coord['y_max'],
                              coord['z_min']:coord['z_max']]

        data_dict['input_metadata'] = seg_pair_slice['input_metadata']
        data_dict['gt_metadata'] = seg_pair_slice['gt_metadata']
        for idx in range(len(data_dict["input"])):
            data_dict['input_metadata'][idx]['data_shape'] = data_shape
        if self.transform is not None:
            data_dict = self.transform(data_dict)
        return data_dict


class DatasetManager(object):
    def __init__(self, dataset, override_transform=None):
        self.dataset = dataset
        self.override_transform = override_transform
        self._transform_state = None

    def __enter__(self):
        if self.override_transform:
            self._transform_state = self.dataset.transform
            self.dataset.transform = self.override_transform
        return self.dataset

    def __exit__(self, *args):
        if self._transform_state:
            self.dataset.transform = self._transform_state


class SCGMChallenge2DTrain(MRI2DSegmentationDataset):
    """This is the Spinal Cord Gray Matter Challenge dataset.

    :param root_dir: the directory containing the training dataset.
    :param site_ids: a list of site ids to filter (i.e. [1, 3]).
    :param subj_ids: the list of subject ids to filter.
    :param rater_ids: the list of the rater ids to filter.
    :param transform: the transformations that should be applied.
    :param cache: if the data should be cached in memory or not.
    :param slice_axis: axis to make the slicing (default axial).

    .. note:: This dataset assumes that you only have one class in your
              ground truth mask (w/ 0's and 1's). It also doesn't
              automatically resample the dataset.

    .. seealso::
        Prados, F., et al (2017). Spinal cord grey matter
        segmentation challenge. NeuroImage, 152, 312–329.
        https://doi.org/10.1016/j.neuroimage.2017.03.010

        Challenge Website:
        http://cmictig.cs.ucl.ac.uk/spinal-cord-grey-matter-segmentation-challenge
    """
    NUM_SITES = 4
    NUM_SUBJECTS = 10
    NUM_RATERS = 4

    def __init__(self, root_dir, slice_axis=2, site_ids=None,
                 subj_ids=None, rater_ids=None, cache=True,
                 transform=None, slice_filter_fn=None,
                 canonical=False, labeled=True):

        self.labeled = labeled
        self.root_dir = root_dir
        self.site_ids = site_ids or range(1, SCGMChallenge2DTrain.NUM_SITES + 1)
        self.subj_ids = subj_ids or range(1, SCGMChallenge2DTrain.NUM_SUBJECTS + 1)
        self.rater_ids = rater_ids or range(1, SCGMChallenge2DTrain.NUM_RATERS + 1)

        self.filename_pairs = []

        for site_id in self.site_ids:
            for subj_id in self.subj_ids:
                if len(self.rater_ids) > 0:
                    for rater_id in self.rater_ids:
                        input_filename = self._build_train_input_filename(site_id, subj_id)
                        gt_filename = self._build_train_input_filename(site_id, subj_id, rater_id)

                        input_filename = os.path.join(self.root_dir, input_filename)
                        gt_filename = os.path.join(self.root_dir, gt_filename)

                        if not self.labeled:
                            gt_filename = None

                        self.filename_pairs.append((input_filename, gt_filename))
                else:
                    input_filename = self._build_train_input_filename(site_id, subj_id)
                    gt_filename = None
                    input_filename = os.path.join(self.root_dir, input_filename)

                    if not self.labeled:
                        gt_filename = None

                    self.filename_pairs.append((input_filename, gt_filename))

        super().__init__(self.filename_pairs, slice_axis, cache,
                         transform, slice_filter_fn, canonical)

    @staticmethod
    def _build_train_input_filename(site_id, subj_id, rater_id=None):
        if rater_id is None:
            return "site{:d}-sc{:02d}-image.nii.gz".format(site_id, subj_id)
        else:
            return "site{:d}-sc{:02d}-mask-r{:d}.nii.gz".format(site_id, subj_id, rater_id)


class SCGMChallenge2DTest(MRI2DSegmentationDataset):
    """This is the Spinal Cord Gray Matter Challenge dataset.

    :param root_dir: the directory containing the test dataset.
    :param site_ids: a list of site ids to filter (i.e. [1, 3]).
    :param subj_ids: the list of subject ids to filter.
    :param transform: the transformations that should be applied.
    :param cache: if the data should be cached in memory or not.
    :param slice_axis: axis to make the slicing (default axial).

    .. note:: This dataset assumes that you only have one class in your
              ground truth mask (w/ 0's and 1's). It also doesn't
              automatically resample the dataset.

    .. seealso::
        Prados, F., et al (2017). Spinal cord grey matter
        segmentation challenge. NeuroImage, 152, 312–329.
        https://doi.org/10.1016/j.neuroimage.2017.03.010

        Challenge Website:
        http://cmictig.cs.ucl.ac.uk/spinal-cord-grey-matter-segmentation-challenge
    """
    NUM_SITES = 4
    NUM_SUBJECTS = 10

    def __init__(self, root_dir, slice_axis=2, site_ids=None,
                 subj_ids=None, cache=True,
                 transform=None, slice_filter_fn=None,
                 canonical=False):

        self.root_dir = root_dir
        self.site_ids = site_ids or range(1, SCGMChallenge2DTest.NUM_SITES + 1)
        self.subj_ids = subj_ids or range(11, 10 + SCGMChallenge2DTest.NUM_SUBJECTS + 1)

        self.filename_pairs = []

        for site_id in self.site_ids:
            for subj_id in self.subj_ids:
                input_filename = self._build_train_input_filename(site_id, subj_id)
                gt_filename = None

                input_filename = os.path.join(self.root_dir, input_filename)
                if not os.path.exists(input_filename):
                    raise RuntimeError("Path '{}' doesn't exist !".format(input_filename))
                self.filename_pairs.append((input_filename, gt_filename))

        super().__init__(self.filename_pairs, slice_axis, cache,
                         transform, slice_filter_fn, canonical)

    @staticmethod
    def _build_train_input_filename(site_id, subj_id, rater_id=None):
        if rater_id is None:
            return "site{:d}-sc{:02d}-image.nii.gz".format(site_id, subj_id)
        else:
            return "site{:d}-sc{:02d}-mask-r{:d}.nii.gz".format(site_id, subj_id, rater_id)


def mt_collate(batch):
    error_msg = "batch must contain tensors, numbers, dicts or lists; found {}"
    elem_type = type(batch[0])
    if torch.is_tensor(batch[0]):
        stacked = torch.stack(batch, 0)
        return stacked
    elif elem_type.__module__ == 'numpy' and elem_type.__name__ != 'str_' \
            and elem_type.__name__ != 'string_':
        elem = batch[0]
        if elem_type.__name__ == 'ndarray':
            # array of string classes and object
            if re.search('[SaUO]', elem.dtype.str) is not None:
                raise TypeError(error_msg.format(elem.dtype))
            return torch.stack([torch.from_numpy(b) for b in batch], 0)
        if elem.shape == ():  # scalars
            py_type = float if elem.dtype.name.startswith('float') else int
            return __numpy_type_map[elem.dtype.name](list(map(py_type, batch)))
    elif isinstance(batch[0], int_classes):
        return torch.LongTensor(batch)
    elif isinstance(batch[0], float):
        return torch.DoubleTensor(batch)
    elif isinstance(batch[0], string_classes):
        return batch
    elif isinstance(batch[0], collections.Mapping):
        return {key: mt_collate([d[key] for d in batch]) for key in batch[0]}
    elif isinstance(batch[0], collections.Sequence):
        transposed = zip(*batch)
        return [mt_collate(samples) for samples in transposed]

    return batch
