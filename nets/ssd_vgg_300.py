# Copyright 2016 Paul Balanca. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""
Definition of 300 VGG-based SSD network.

This model was initially introduced in:
SSD: Single Shot MultiBox Detector
Wei Liu, Dragomir Anguelov, Dumitru Erhan, Christian Szegedy, Scott Reed,
Cheng-Yang Fu, Alexander C. Berg
https://arxiv.org/abs/1512.02325

Two variants of the model are defined: the 300x300 and 512x512 models, the
latter obtaining a slightly better accuracy on Pascal VOC.

Usage:
    with slim.arg_scope(ssd_vgg.ssd_vgg()):
        outputs, end_points = ssd_vgg.ssd_vgg(inputs)

This network port of the original Caffe model. The padding in TF and Caffe
is slightly different, and can lead to severe accuracy drop if not taken care
in a correct way!

In Caffe, the output size of convolution and pooling layers are computing as
following: h_o = (h_i + 2 * pad_h - kernel_h) / stride_h + 1

Nevertheless, there is a subtle difference between both for stride > 1. In
the case of convolution:
    top_size = floor((bottom_size + 2*pad - kernel_size) / stride) + 1
whereas for pooling:
    top_size = ceil((bottom_size + 2*pad - kernel_size) / stride) + 1
Hence implicitely allowing some additional padding even if pad = 0. This
behaviour explains why pooling with stride and kernel of size 2 are behaving
the same way in TensorFlow and Caffe.

Nevertheless, this is not the case anymore for other kernel sizes, hence
motivating the use of special padding layer for controlling these side-effects.

@@ssd_vgg_300
"""

import math
from collections import namedtuple

import numpy as np
import tensorflow as tf

import tf_extend as tfe
from nets import custom_layers
from nets import ssd_common

import tensorflow.contrib.slim as slim


# =========================================================================== #
# SSD class definition.
# =========================================================================== #
SSDParams = namedtuple('SSDParameters', ['img_shape', 'num_classes', 'no_annotation_label', 'feat_layers',
                                         'feat_shapes', 'anchor_size_bounds', 'anchor_sizes', 'anchor_ratios',
                                         'anchor_steps', 'anchor_offset', 'normalizations', 'prior_scaling' ])


class SSDNet(object):
    """Implementation of the SSD VGG-based 300 network.

    The default features layers with 300x300 image input are:
      conv4 ==> 38 x 38
      conv7 ==> 19 x 19
      conv8 ==> 10 x 10
      conv9 ==> 5 x 5
      conv10 ==> 3 x 3
      conv11 ==> 1 x 1
    The default image size used to train this network is 300x300.
    """
    default_params = SSDParams(
        img_shape=(300, 300), num_classes=21, no_annotation_label=21,
        feat_layers=['block4', 'block7', 'block8', 'block9', 'block10', 'block11'],
        feat_shapes=[(38, 38), (19, 19), (10, 10), (5, 5), (3, 3), (1, 1)],
        anchor_size_bounds=[0.15, 0.90],
        anchor_sizes=[(21., 45.0), (45.0, 99.), (99.0, 153.0), (153.0, 207.0), (207.0, 261.0), (261.0, 315.0)],
        anchor_ratios=[[2, 0.5], [2, 0.5, 3, 1.0/3], [2, 0.5, 3, 1.0/3], [2, 0.5, 3, 1.0/3], [2, 0.5], [2, 0.5]],
        anchor_steps=[8, 16, 32, 64, 100, 300],
        anchor_offset=0.5,
        normalizations=[20, -1, -1, -1, -1, -1],
        prior_scaling=[0.1, 0.1, 0.2, 0.2])

    def __init__(self, params=None):
        """
        Init the SSD net with some parameters. Use the default ones if none provided.
        """
        if isinstance(params, SSDParams):
            self.params = params
        else:
            self.params = SSDNet.default_params
        pass

    # ======================================================================= #
    def net(self, inputs, is_training=True, update_feat_shapes=True, dropout_keep_prob=0.5,
            prediction_fn=slim.softmax, reuse=None, scope='ssd_300_vgg'):
        """SSD network definition.
        """
        r = ssd_net(inputs,
                    num_classes=self.params.num_classes,
                    feat_layers=self.params.feat_layers,
                    anchor_sizes=self.params.anchor_sizes,
                    anchor_ratios=self.params.anchor_ratios,
                    normalizations=self.params.normalizations,
                    is_training=is_training, dropout_keep_prob=dropout_keep_prob,
                    prediction_fn=prediction_fn, reuse=reuse, scope=scope)
        # Update feature shapes (try at least!)
        if update_feat_shapes:
            shapes = ssd_feat_shapes_from_net(r[0], self.params.feat_shapes)
            self.params = self.params._replace(feat_shapes=shapes)
        return r

    def arg_scope(self, weight_decay=0.0005, data_format='NHWC'):
        """Network arg_scope.
        """
        return ssd_arg_scope(weight_decay, data_format=data_format)

    def arg_scope_caffe(self, caffe_scope):
        """Caffe arg_scope used for weights importing.
        """
        return ssd_arg_scope_caffe(caffe_scope)

    # ======================================================================= #
    def update_feature_shapes(self, predictions):
        """Update feature shapes from predictions collection (Tensor or Numpy
        array).
        """
        shapes = ssd_feat_shapes_from_net(predictions, self.params.feat_shapes)
        self.params = self.params._replace(feat_shapes=shapes)
        pass

    # 前两个为所有“default boxes中心”的坐标，后两个为【原始比例，放大比例、不同的宽高比例...】
    def anchors(self, img_shape, dtype=np.float32):
        """Compute the default anchor boxes, given an image shape.
        """
        return ssd_anchors_all_layers(
            img_shape, self.params.feat_shapes, self.params.anchor_sizes,
            self.params.anchor_ratios, self.params.anchor_steps, self.params.anchor_offset, dtype)

    def bboxes_encode(self, labels, bboxes, anchors, scope=None):
        """Encode labels and bounding boxes.
        """
        return ssd_common.tf_ssd_bboxes_encode(
            labels, bboxes, anchors, self.params.num_classes, self.params.no_annotation_label,
            ignore_threshold=0.5, prior_scaling=self.params.prior_scaling, scope=scope)

    def bboxes_decode(self, feat_localizations, anchors, scope='ssd_bboxes_decode'):
        """Encode labels and bounding boxes.
        """
        return ssd_common.tf_ssd_bboxes_decode(
            feat_localizations, anchors, prior_scaling=self.params.prior_scaling, scope=scope)

    def detected_bboxes(self, predictions, localisations, select_threshold=None, nms_threshold=0.5,
                        clipping_bbox=None, top_k=400, keep_top_k=200):
        """Get the detected bounding boxes from the SSD network output.
        """
        # Select top_k bboxes from predictions, and clip
        rscores, rbboxes = ssd_common.tf_ssd_bboxes_select(
            predictions, localisations, select_threshold=select_threshold, num_classes=self.params.num_classes)
        rscores, rbboxes = tfe.bboxes_sort(rscores, rbboxes, top_k=top_k)
        # Apply NMS algorithm.
        rscores, rbboxes = tfe.bboxes_nms_batch(rscores, rbboxes, nms_threshold=nms_threshold, keep_top_k=keep_top_k)
        if clipping_bbox is not None:
            rbboxes = tfe.bboxes_clip(clipping_bbox, rbboxes)
        return rscores, rbboxes

    def losses(self, logits, localisations, gclasses, glocalisations, gscores, match_threshold=0.5,
               negative_ratio=3., alpha=1., label_smoothing=0., scope='ssd_losses'):
        """Define the SSD network losses.
        """
        return ssd_losses(logits, localisations, gclasses, glocalisations, gscores, match_threshold=match_threshold,
                          negative_ratio=negative_ratio, alpha=alpha, label_smoothing=label_smoothing, scope=scope)

    pass


# =========================================================================== #
# SSD tools...
# =========================================================================== #
def ssd_size_bounds_to_values(size_bounds, n_feat_layers, img_shape=(300, 300)):
    """Compute the reference sizes of the anchor boxes from relative bounds.
    The absolute values are measured in pixels, based on the network
    default size (300 pixels).

    This function follows the computation performed in the original
    implementation of SSD in Caffe.

    Return:
      list of list containing the absolute sizes at each scale. For each scale,
      the ratios only apply to the first value.
    """
    assert img_shape[0] == img_shape[1]

    img_size = img_shape[0]
    min_ratio = int(size_bounds[0] * 100)
    max_ratio = int(size_bounds[1] * 100)
    step = int(math.floor((max_ratio - min_ratio) / (n_feat_layers - 2)))
    # Start with the following smallest sizes.
    sizes = [[img_size * size_bounds[0] / 2, img_size * size_bounds[0]]]
    for ratio in range(min_ratio, max_ratio + 1, step):
        sizes.append((img_size * ratio / 100., img_size * (ratio + step) / 100.))
    return sizes


# 获取合适的shapes
def ssd_feat_shapes_from_net(predictions, default_shapes=None):
    """Try to obtain the feature shapes from the prediction layers. The latter
    can be either a Tensor or Numpy ndarray.

    Return:
      list of feature shapes. Default values if predictions shape not fully
      determined.
    """
    feat_shapes = []
    for l in predictions:
        # Get the shape, from either a np array or a tensor.
        if isinstance(l, np.ndarray):
            shape = l.shape
        else:
            shape = l.get_shape().as_list()
        shape = shape[1:4]
        # Problem: undetermined shape...
        if None in shape:
            return default_shapes
        else:
            feat_shapes.append(shape)
    return feat_shapes


def ssd_anchor_one_layer(img_shape, feat_shape, sizes, ratios, step, offset=0.5, dtype=np.float32):
    """Computer SSD default anchor boxes for one feature layer.

    Determine the relative position grid of the centers, and the relative
    width and height.

    Arguments:
      feat_shape: Feature shape, used for computing relative position grids;
      size: Absolute reference sizes; 这里的size是一个tuple（该层的尺寸，下一层的尺寸），因为根据论文所说，ratio为1的会产生两个框，s'_k, scale is s'_k=sqrt(s_k*s_k+1)，这是额外的额外的一个附加框
      ratios: Ratios to use on these features;
      img_shape: Image shape, used for computing height, width relatively to the
        former;
      offset: Grid offset.

    Return:
      y, x, h, w: Relative x and y grids, and height and width.
    """

    # Compute the position grid: simple way.
    # y, x = np.mgrid[0:feat_shape[0], 0:feat_shape[1]]
    # y = (y.astype(dtype) + offset) / feat_shape[0]
    # x = (x.astype(dtype) + offset) / feat_shape[1]
    # Weird SSD-Caffe computation using steps values...
    # 对应的特征图上每个点的框的横纵坐标
    y, x = np.mgrid[0:feat_shape[0], 0:feat_shape[1]]
    y = (y.astype(dtype) + offset) * step / img_shape[0]
    x = (x.astype(dtype) + offset) * step / img_shape[1]

    # Expand dims to support easy broadcasting.
    # 这里扩展了维度，因为tf_ssd_bboxes_encode_layer是要用到。
    y = np.expand_dims(y, axis=-1)
    x = np.expand_dims(x, axis=-1)

    # Compute relative height and width.
    # Tries to follow the original implementation of SSD for the order.
    num_anchors = len(sizes) + len(ratios)
    h = np.zeros((num_anchors, ), dtype=dtype)  # 对应的特征图上每个点的长宽
    w = np.zeros((num_anchors, ), dtype=dtype)
    # Add first anchor boxes with ratio=1.
    # 论文种的s_k等于=sizes[0] / img_shape[0]
    h[0] = sizes[0] / img_shape[0]  # sizes[1]是下一层的大小，只有size[0]是当前层的。
    w[0] = sizes[0] / img_shape[1]  # h[0],w[0]是默认框占整个图片（300*300）的比例。即`aspect ratio is 1`.
    di = 1
    if len(sizes) > 1:  # 论文种的s'_k, scale is s'_k=sqrt(s_k*s_k+1)
        h[1] = math.sqrt(sizes[0] * sizes[1]) / img_shape[0]
        w[1] = math.sqrt(sizes[0] * sizes[1]) / img_shape[1]
        di += 1
    for i, r in enumerate(ratios):  # aspect ratio is r
        h[i+di] = sizes[0] / img_shape[0] / math.sqrt(r)
        w[i+di] = sizes[0] / img_shape[1] * math.sqrt(r)
    return y, x, h, w


def ssd_anchors_all_layers(img_shape, layers_shape, anchor_sizes, anchor_ratios,
                           anchor_steps, offset=0.5, dtype=np.float32):
    """Compute anchor boxes for all feature layers.
    """
    layers_anchors = []
    for i, s in enumerate(layers_shape):
        anchor_bboxes = ssd_anchor_one_layer(
            img_shape, s, anchor_sizes[i], anchor_ratios[i], anchor_steps[i], offset=offset, dtype=dtype)
        layers_anchors.append(anchor_bboxes)
    return layers_anchors


# =========================================================================== #
# Functional definition of VGG-based SSD 300.
# =========================================================================== #
def tensor_shape(x, rank=3):
    """Returns the dimensions of a tensor.
    Args:
      image: A N-D Tensor of shape.
    Returns:
      A list of dimensions. Dimensions that are statically known are python
        integers,otherwise they are integer scalar tensors.
    """
    if x.get_shape().is_fully_defined():
        return x.get_shape().as_list()
    else:
        static_shape = x.get_shape().with_rank(rank).as_list()
        dynamic_shape = tf.unstack(tf.shape(x), rank)
        return [s if s is not None else d for s, d in zip(static_shape, dynamic_shape)]

    pass


def ssd_multibox_layer(inputs, num_classes, sizes, ratios=list([1]), normalization=-1, bn_normalization=False):
    """Construct a multibox layer, return a class and localization predictions.
    """
    net = inputs
    if normalization > 0:
        net = custom_layers.l2_normalization(net, scaling=True)
    # Number of anchors., 两种尺寸，每种都有缩放
    num_anchors = len(sizes) + len(ratios)

    # Location.：比如第一层：38 * 38 * 4 * 4，每一个点（28 * 38）的num_anchors（4）种框的四个坐标值（4，x_min, x_max, y_min, y_max）
    num_loc_pred = num_anchors * 4
    loc_pred = slim.conv2d(net, num_loc_pred, [3, 3], activation_fn=None, scope='conv_loc')
    loc_pred = custom_layers.channel_to_last(loc_pred)
    # 特征图的每个点都有每个尺寸的各个缩放比的框
    loc_pred = tf.reshape(loc_pred, tensor_shape(loc_pred, 4)[:-1]+[num_anchors, 4])   # （38,38,num_anchors,4）

    # Class prediction.: 比如第一层：38 * 38 * 4 * 21，每一个点（28 * 38）的num_anchors（4）种框的每一类预测得分（21）
    num_cls_pred = num_anchors * num_classes
    cls_pred = slim.conv2d(net, num_cls_pred, [3, 3], activation_fn=None, scope='conv_cls')
    cls_pred = custom_layers.channel_to_last(cls_pred)
    cls_pred = tf.reshape(cls_pred, tensor_shape(cls_pred, 4)[:-1]+[num_anchors, num_classes])
    return cls_pred, loc_pred



def ssd_net(inputs, num_classes=SSDNet.default_params.num_classes, feat_layers=SSDNet.default_params.feat_layers,
            anchor_sizes=SSDNet.default_params.anchor_sizes, anchor_ratios=SSDNet.default_params.anchor_ratios,
            normalizations=SSDNet.default_params.normalizations, is_training=True, dropout_keep_prob=0.5,
            prediction_fn=slim.softmax, reuse=None, scope='ssd_300_vgg'):
    """
    SSD net definition.
    """

    # End_points collect relevant activations for external use.
    end_points = {}
    with tf.variable_scope(scope, 'ssd_300_vgg', [inputs], reuse=reuse):
        # 基础 VGG-16 blocks.
        net = slim.repeat(inputs, 2, slim.conv2d, 64, [3, 3], scope='conv1')
        end_points['block1'] = net
        net = slim.max_pool2d(net, [2, 2], scope='pool1')  # 150*150*64
        # Block 2.
        net = slim.repeat(net, 2, slim.conv2d, 128, [3, 3], scope='conv2')
        end_points['block2'] = net
        net = slim.max_pool2d(net, [2, 2], scope='pool2')  # 75*75*128
        # Block 3.
        net = slim.repeat(net, 3, slim.conv2d, 256, [3, 3], scope='conv3')
        end_points['block3'] = net
        net = slim.max_pool2d(net, [2, 2], scope='pool3')  # 38*38*256
        # Block 4.
        net = slim.repeat(net, 3, slim.conv2d, 512, [3, 3], scope='conv4')
        end_points['block4'] = net
        net = slim.max_pool2d(net, [2, 2], scope='pool4')  # 19*19*512
        # Block 5.
        net = slim.repeat(net, 3, slim.conv2d, 512, [3, 3], scope='conv5')
        end_points['block5'] = net
        net = slim.max_pool2d(net, [3, 3], stride=1, scope='pool5')  # 19*19*512

        # 添加的 SSD blocks.
        # Block 6: let's dilate the hell out of it!
        net = slim.conv2d(net, 1024, [3, 3], rate=6, scope='conv6')
        end_points['block6'] = net
        net = tf.layers.dropout(net, rate=dropout_keep_prob, training=is_training)
        # Block 7: 1x1 conv. Because the fuck.
        net = slim.conv2d(net, 1024, [1, 1], scope='conv7')
        end_points['block7'] = net
        net = tf.layers.dropout(net, rate=dropout_keep_prob, training=is_training)

        # Block 8/9/10/11: 1x1 and 3x3 convolutions stride 2 (except lasts).
        end_point = 'block8'
        with tf.variable_scope(end_point):
            net = slim.conv2d(net, 256, [1, 1], scope='conv1x1')
            net = custom_layers.pad2d(net, pad=(1, 1))
            net = slim.conv2d(net, 512, [3, 3], stride=2, scope='conv3x3', padding='VALID')
        end_points[end_point] = net
        end_point = 'block9'
        with tf.variable_scope(end_point):
            net = slim.conv2d(net, 128, [1, 1], scope='conv1x1')
            net = custom_layers.pad2d(net, pad=(1, 1))
            net = slim.conv2d(net, 256, [3, 3], stride=2, scope='conv3x3', padding='VALID')
        end_points[end_point] = net
        end_point = 'block10'
        with tf.variable_scope(end_point):
            net = slim.conv2d(net, 128, [1, 1], scope='conv1x1')
            net = slim.conv2d(net, 256, [3, 3], scope='conv3x3', padding='VALID')
        end_points[end_point] = net
        end_point = 'block11'
        with tf.variable_scope(end_point):
            net = slim.conv2d(net, 128, [1, 1], scope='conv1x1')
            net = slim.conv2d(net, 256, [3, 3], scope='conv3x3', padding='VALID')
        end_points[end_point] = net

        # Prediction and localisations layers.
        predictions = []
        logits = []
        localisations = []
        # 每一层特征图的预测
        for i, layer in enumerate(feat_layers):
            with tf.variable_scope(layer + '_box'):
                # 特征图的需要框的点数*每个点的框数
                # 每一层特征图框的大小和框的变化已经定好了
                pred, loc = ssd_multibox_layer(end_points[layer], num_classes,
                                          anchor_sizes[i], anchor_ratios[i], normalizations[i])
            predictions.append(prediction_fn(pred))
            logits.append(pred)
            localisations.append(loc)

        return predictions, localisations, logits, end_points

    pass

ssd_net.default_image_size = 300


def ssd_arg_scope(weight_decay=0.0005, data_format='NHWC'):
    """Defines the VGG arg scope.

    Args:
      weight_decay: The l2 regularization coefficient.

    Returns:
      An arg_scope.
    """
    with slim.arg_scope([slim.conv2d, slim.fully_connected], activation_fn=tf.nn.relu,
                        weights_regularizer=slim.l2_regularizer(weight_decay),
                        weights_initializer=tf.contrib.layers.xavier_initializer(),
                        biases_initializer=tf.zeros_initializer()):
        with slim.arg_scope([slim.conv2d, slim.max_pool2d], padding='SAME', data_format=data_format):
            with slim.arg_scope([custom_layers.pad2d, custom_layers.l2_normalization, custom_layers.channel_to_last],
                                data_format=data_format) as sc:
                return sc
    pass


# =========================================================================== #
# Caffe scope: importing weights at initialization.
# =========================================================================== #
def ssd_arg_scope_caffe(caffe_scope):
    """Caffe scope definition.

    Args:
      caffe_scope: Caffe scope object with loaded weights.

    Returns:
      An arg_scope.
    """
    # Default network arg scope.
    with slim.arg_scope([slim.conv2d], activation_fn=tf.nn.relu, weights_initializer=caffe_scope.conv_weights_init(),
                        biases_initializer=caffe_scope.conv_biases_init()):
        with slim.arg_scope([slim.fully_connected], activation_fn=tf.nn.relu):
            with slim.arg_scope([custom_layers.l2_normalization], scale_initializer=caffe_scope.l2_norm_scale_init()):
                with slim.arg_scope([slim.conv2d, slim.max_pool2d], padding='SAME') as sc:
                    return sc
    pass


# =========================================================================== #
# SSD loss function.
# =========================================================================== #
def ssd_losses(logits, localisations, gclasses, glocalisations, gscores, match_threshold=0.5,
               negative_ratio=3., alpha=1., label_smoothing=0., scope=None):
    with tf.name_scope(scope, 'ssd_losses'):
        lshape = tfe.get_shape(logits[0], 5)
        num_classes = lshape[-1]
        batch_size = lshape[0]

        # Flatten out all vectors!
        flogits = []
        fgclasses = []
        fgscores = []
        flocalisations = []
        fglocalisations = []
        for i in range(len(logits)):
            flogits.append(tf.reshape(logits[i], [-1, num_classes]))
            fgclasses.append(tf.reshape(gclasses[i], [-1]))
            fgscores.append(tf.reshape(gscores[i], [-1]))
            flocalisations.append(tf.reshape(localisations[i], [-1, 4]))
            fglocalisations.append(tf.reshape(glocalisations[i], [-1, 4]))
        # And concat the crap!
        logits = tf.concat(flogits, axis=0)
        gclasses = tf.concat(fgclasses, axis=0)
        gscores = tf.concat(fgscores, axis=0)
        localisations = tf.concat(flocalisations, axis=0)
        glocalisations = tf.concat(fglocalisations, axis=0)
        dtype = logits.dtype

        # Compute positive matching mask... 正样本
        pmask = gscores > match_threshold
        fpmask = tf.cast(pmask, dtype)
        n_positives = tf.reduce_sum(fpmask)

        # Hard negative mining...
        no_classes = tf.cast(pmask, tf.int32)
        predictions = slim.softmax(logits)
        nmask = tf.logical_and(tf.logical_not(pmask), gscores > -0.5)  # 这里存疑，为什么是-0.5？，论文中说的是0.5
        fnmask = tf.cast(nmask, dtype)
        nvalues = tf.where(nmask, predictions[:, 0], 1. - fnmask)
        nvalues_flat = tf.reshape(nvalues, [-1])
        # Number of negative entries to select.
        max_neg_entries = tf.cast(tf.reduce_sum(fnmask), tf.int32)
        n_neg = tf.cast(negative_ratio * n_positives, tf.int32) + batch_size
        n_neg = tf.minimum(n_neg, max_neg_entries)

        val, idxes = tf.nn.top_k(-nvalues_flat, k=n_neg)
        max_hard_pred = -val[-1]
        # Final negative mask.
        nmask = tf.logical_and(nmask, nvalues < max_hard_pred)
        fnmask = tf.cast(nmask, dtype)

        # Add cross-entropy loss.
        with tf.name_scope('cross_entropy_pos'):
            loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=gclasses)
            loss = tf.div(tf.reduce_sum(loss * fpmask), batch_size, name='value')
            tf.losses.add_loss(loss)

        with tf.name_scope('cross_entropy_neg'):
            # 从不是正样本的框里面选择， 让他们预测是背景的概率
            loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=no_classes)
            # 预测背景的置信度越小，误差越大，  误差变小说的是，是背景要预测成背景
            loss = tf.div(tf.reduce_sum(loss * fnmask), batch_size, name='value')
            tf.losses.add_loss(loss)


        # Add localization loss: smooth L1, L2, ...
        with tf.name_scope('localization'):
            # Weights Tensor: positive mask + random negative.
            weights = tf.expand_dims(alpha * fpmask, axis=-1)
            loss = custom_layers.abs_smooth(localisations - glocalisations)
            loss = tf.div(tf.reduce_sum(loss * weights), batch_size, name='value')
            tf.losses.add_loss(loss)
        pass

    pass

