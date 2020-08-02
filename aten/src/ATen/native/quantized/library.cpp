#include <torch/library.h>

#include <ATen/native/quantized/cpu/conv_packed_params.h>
#include <ATen/native/quantized/cpu/packed_params.h>
#include <torch/custom_class.h>

torch::jit::class_<LinearPackedParamsBase> register_linear_params();

template <int kSpatialDim = 2>
torch::jit::class_<ConvPackedParamsBase<kSpatialDim>> register_conv_params();

extern template torch::jit::class_<ConvPackedParamsBase<2>> register_conv_params<2>();
extern template torch::jit::class_<ConvPackedParamsBase<3>> register_conv_params<3>();

TORCH_LIBRARY(quantized, m) {
  register_linear_params();
  register_conv_params<2>();
  register_conv_params<3>();

  m.def("add(Tensor qa, Tensor qb, float scale, int zero_point) -> Tensor qc");
  m.def("add_relu(Tensor qa, Tensor qb, float scale, int zero_point) -> Tensor qc");
  m.def("add_out(Tensor qa, Tensor qb, Tensor(a!) out) -> Tensor(a!) out");
  m.def("add_relu_out(Tensor qa, Tensor qb, Tensor(a!) out) -> Tensor(a!) out");
  m.def("add_scalar(Tensor qa, Scalar b) -> Tensor qc");
  m.def("add_scalar_relu(Tensor qa, Scalar b) -> Tensor qc");
  m.def("add_scalar_out(Tensor qa, Scalar b, Tensor(a!) out) -> Tensor(a!) out");
  m.def("add_scalar_relu_out(Tensor qa, Scalar b, Tensor(a!) out) -> Tensor(a!) out");
  // TODO: remove after broadcasting is supported
  m.def("add_scalar_out.Tensor(Tensor qa, Tensor b, Tensor(a!) out) -> Tensor(a!) out");
  m.def("add_scalar.Tensor(Tensor qa, Tensor b) -> Tensor qc");
  m.def("add_scalar_relu.Tensor(Tensor qa, Tensor b) -> Tensor qc");
  m.def("add_scalar_relu_out.Tensor(Tensor qa, Tensor b, Tensor(a!) out) -> Tensor(a!) out");
  // This is needed for graph mode quantization, when we fuse
  // dequant - aten::batch_norm - quant into quantized::batch_norm
  // and dimension is unknown given only the aten op call
  // quantized::batch_norm supports both 2d and 3d batch norm right now
  // it should also support 1d batch_norm after quantized::batch_norm1d is
  // implemented
  m.def("batch_norm(Tensor qx, Tensor? weight, Tensor? bias, Tensor mean, Tensor var, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("batch_norm_relu(Tensor qx, Tensor? weight, Tensor? bias, Tensor mean, Tensor var, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("batch_norm2d(Tensor qx, Tensor? weight, Tensor? bias, Tensor mean, Tensor var, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("batch_norm2d_relu(Tensor qx, Tensor? weight, Tensor? bias, Tensor mean, Tensor var, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("batch_norm3d(Tensor qx, Tensor? weight, Tensor? bias, Tensor mean, Tensor var, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("batch_norm3d_relu(Tensor qx, Tensor? weight, Tensor? bias, Tensor mean, Tensor var, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("clamp(Tensor qx, Scalar? min, Scalar? max) -> Tensor qy");
  m.def("threshold(Tensor qx, Scalar threshold, Scalar value) -> Tensor qy");
  m.def("cat(Tensor[] qx, int dim, float? scale, int? zero_point) -> Tensor");
  m.def("cat_relu(Tensor[] qx, int dim, float? scale, int? zero_point) -> Tensor");
  m.def("cat_out(Tensor[] qx, int dim, Tensor(a!) out) -> Tensor(a!)");
  m.def("cat_relu_out(Tensor[] qx, int dim, Tensor(a!) out) -> Tensor(a!)");
  m.def("conv1d(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv1d_relu(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv2d.new(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv2d_relu.new(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv3d.new(Tensor qx, __torch__.torch.classes.quantized.Conv3dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv3d_relu.new(Tensor qx, __torch__.torch.classes.quantized.Conv3dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv2d(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase weight, int[] stride, int[] padding, int[] dilation, int groups, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv2d_relu(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase weight, int[] stride, int[] padding, int[] dilation, int groups, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv3d(Tensor qx, __torch__.torch.classes.quantized.Conv3dPackedParamsBase weight, int[] stride, int[] padding, int[] dilation, int groups, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv3d_relu(Tensor qx, __torch__.torch.classes.quantized.Conv3dPackedParamsBase weight, int[] stride, int[] padding, int[] dilation, int groups, float output_scale, int output_zero_point) -> Tensor");
  // conv_prepack is deprecated, please use conv2d_prepack for 2D conv.
  m.def("conv_prepack(Tensor weight, Tensor? bias, int[] stride, int[] padding, int[] dilation, int groups) -> __torch__.torch.classes.quantized.Conv2dPackedParamsBase");
  m.def("conv1d_prepack(Tensor weight, Tensor? bias, int[] stride, int[] padding, int[] dilation, int groups) -> __torch__.torch.classes.quantized.Conv2dPackedParamsBase");
  m.def("conv2d_prepack(Tensor weight, Tensor? bias, int[] stride, int[] padding, int[] dilation, int groups) -> __torch__.torch.classes.quantized.Conv2dPackedParamsBase");
  m.def("conv3d_prepack(Tensor weight, Tensor? bias, int[] stride, int[] padding, int[] dilation, int groups) -> __torch__.torch.classes.quantized.Conv3dPackedParamsBase");
  // conv_unpack is deprecated, please use conv2d_unpack for 2D conv.
  m.def("conv_unpack(__torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weights) -> (Tensor unpacked_weights, Tensor? B_origin)");
  m.def("conv1d_unpack(__torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weights) -> (Tensor unpacked_weights, Tensor? B_origin)");
  m.def("conv2d_unpack(__torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weights) -> (Tensor unpacked_weights, Tensor? B_origin)");
  m.def("conv3d_unpack(__torch__.torch.classes.quantized.Conv3dPackedParamsBase packed_weights) -> (Tensor unpacked_weights, Tensor? B_origin)");
  m.def("conv2d_stride(__torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weights) -> int[]");
  m.def("conv2d_padding(__torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weights) -> int[]");
  m.def("conv2d_dilation(__torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weights) -> int[]");
  m.def("conv2d_groups(__torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weights) -> int");
  m.def("conv3d_stride(__torch__.torch.classes.quantized.Conv3dPackedParamsBase packed_weights) -> int[]");
  m.def("conv3d_padding(__torch__.torch.classes.quantized.Conv3dPackedParamsBase packed_weights) -> int[]");
  m.def("conv3d_dilation(__torch__.torch.classes.quantized.Conv3dPackedParamsBase packed_weights) -> int[]");
  m.def("conv3d_groups(__torch__.torch.classes.quantized.Conv3dPackedParamsBase packed_weights) -> int");
  m.def("elu(Tensor self, float output_scale, int output_zero_point, Scalar alpha=1, Scalar scale=1, Scalar input_scale=1) -> Tensor");
  m.def("hardswish(Tensor input, float output_scale, int output_zero_point) -> Tensor");
  m.def("group_norm(Tensor input, int num_groups, Tensor? weight, Tensor? bias, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("instance_norm(Tensor input, Tensor? weight, Tensor? bias, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def("layer_norm(Tensor input, int[] normalized_shape, Tensor? weight, Tensor? bias, float eps, float output_scale, int output_zero_point) -> Tensor");
  m.def(
      "linear(Tensor X, __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack, float Y_scale_i, int Y_zero_point_i) -> Tensor Y");
  m.def(
      "linear_relu(Tensor X, __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack, float Y_scale_i, int Y_zero_point_i) -> Tensor Y");
  m.def(
      "linear_dynamic(Tensor X, __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack, bool reduce_range=False) -> Tensor Y");
  m.def(
      "linear_relu_dynamic(Tensor X, __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack, bool reduce_range=False) -> Tensor Y");
  m.def(
      "linear_dynamic_fp16(Tensor X, __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack) -> Tensor Y");
  m.def(
      "linear_prepack(Tensor W, Tensor? B=None) -> __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack");
  m.def(
      "linear_prepack_fp16(Tensor W, Tensor? B=None) -> __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack");
  m.def("linear_prepack_legacy(Tensor W, Tensor? B=None) -> Tensor W_prepack");
  m.def(
      "linear_prepack_fp16_legacy(Tensor W, Tensor? B=None) -> Tensor W_prepack");
  m.def(
      "linear_unpack(__torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack) -> (Tensor W_origin, Tensor? B_origin)");
  m.def(
      "linear_unpack_fp16(__torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack) -> (Tensor W_origin, Tensor? B_origin)");
  m.def(
      "linear_unpack.legacy(Tensor W_prepack) -> (Tensor W_origin, Tensor? B_origin)");
  m.def(
      "linear_unpack_fp16.legacy(Tensor W_prepack) -> (Tensor W_origin, Tensor? B_origin)");
  m.def("mul(Tensor qa, Tensor qb, float scale, int zero_point)-> Tensor qc");
  m.def("mul_relu(Tensor qa, Tensor qb, float scale, int zero_point)-> Tensor qc");
  m.def("mul_out(Tensor qa, Tensor qb, Tensor(a!) out)-> Tensor(a!) out");
  m.def("mul_relu_out(Tensor qa, Tensor qb, Tensor(a!) out)-> Tensor(a!) out");
  m.def("mul_scalar(Tensor qa, Scalar b)-> Tensor qc");
  m.def("mul_scalar_relu(Tensor qa, Scalar b)-> Tensor qc");
  m.def("mul_scalar_out(Tensor qa, Scalar b, Tensor(a!) out)-> Tensor(a!) out");
  m.def("mul_scalar_relu_out(Tensor qa, Scalar b, Tensor(a!) out)-> Tensor(a!) out");
  // TODO: remove after broadcasting is supported
  m.def("mul_scalar.Tensor(Tensor qa, Tensor b)-> Tensor qc");
  m.def("mul_scalar_relu.Tensor(Tensor qa, Tensor b)-> Tensor qc");
  m.def("mul_scalar_out.Tensor(Tensor qa, Tensor b, Tensor(a!) out)-> Tensor(a!) out");
  m.def("mul_scalar_relu_out.Tensor(Tensor qa, Tensor b, Tensor(a!) out)-> Tensor(a!) out");
  // NB: missing a space after comma here...
  m.def("max_pool2d(Tensor qx, int[] kernel_size, int[] stride, int[] padding, int[] dilation,bool ceil_mode) -> Tensor");
  m.def("relu6(Tensor qx, bool inplace=False) -> Tensor");
}

// According to #33294: The "_" prefix registration will be
// removed when the operators are all migrated to mobile.
// https://github.com/pytorch/pytorch/issues/36510
TORCH_LIBRARY(_quantized, m) {
  m.def("add(Tensor qa, Tensor qb, float scale, int zero_point) -> Tensor qc");
  m.def("conv2d(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv2d_relu(Tensor qx, __torch__.torch.classes.quantized.Conv2dPackedParamsBase packed_weight, float output_scale, int output_zero_point) -> Tensor");
  m.def("conv2d_prepack(Tensor weight, Tensor? bias, int[] stride, int[] padding, int[] dilation, int groups) -> __torch__.torch.classes.quantized.Conv2dPackedParamsBase");
  m.def(
      "linear(Tensor X, __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack, float Y_scale_i, int Y_zero_point_i) -> Tensor Y");
  m.def(
      "linear_dynamic(Tensor X, __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack, bool reduce_range=False) -> Tensor Y");
  m.def(
      "linear_prepack(Tensor W, Tensor? B=None) -> __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack");
  m.def(
      "linear_prepack_fp16(Tensor W, Tensor? B=None) -> __torch__.torch.classes.quantized.LinearPackedParamsBase W_prepack");
  m.def("linear_prepack_legacy(Tensor W, Tensor? B=None) -> Tensor W_prepack");
  m.def(
      "linear_prepack_fp16_legacy(Tensor W, Tensor? B=None) -> Tensor W_prepack");
}
