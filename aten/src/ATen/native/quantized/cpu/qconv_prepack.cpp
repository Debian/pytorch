#include <array>
#include <vector>

#include <ATen/ATen.h>
#include <ATen/native/quantized/cpu/conv_packed_params.h>
#include <ATen/native/quantized/cpu/fbgemm_utils.h>
#include <ATen/native/quantized/cpu/init_qnnpack.h>
#include <ATen/native/quantized/cpu/qnnpack_utils.h>
#include <ATen/native/quantized/cpu/quant_utils.h>
#include <ATen/quantized/Quantizer.h>
#include <torch/library.h>

#ifdef USE_FBGEMM
template <int kSpatialDim>
c10::intrusive_ptr<ConvPackedParamsBase<kSpatialDim>> PackedConvWeight<
    kSpatialDim>::
    prepack(
        at::Tensor weight,
        c10::optional<at::Tensor> bias,
        torch::List<int64_t> stride,
        torch::List<int64_t> padding,
        torch::List<int64_t> dilation,
        int64_t groups) {
  TORCH_CHECK(
      weight.ndimension() == kSpatialDim + 2,
      "Weights are expected to have ",
      kSpatialDim + 2,
      " dimensions");

  TORCH_CHECK(
      stride.size() == kSpatialDim,
      "stride should contain ",
      kSpatialDim,
      " elements for ",
      kSpatialDim,
      "D convolution.");
  TORCH_CHECK(
      padding.size() == kSpatialDim,
      "Specify front/top/left padding only. "
      "end/bottom/right padding assumed to be equal to front/top/left");
  TORCH_CHECK(
      dilation.size() == kSpatialDim,
      "dilation should contain ",
      kSpatialDim,
      " elements for ",
      kSpatialDim,
      "D convolution.");
  const int output_channels = weight.size(0);
  const int input_channels_per_group = weight.size(1);
  const int kernel_d = kSpatialDim == 2 ? 1 : weight.size(2);
  const int kernel_h = weight.size(kSpatialDim);
  const int kernel_w = weight.size(kSpatialDim + 1);

  // mini-batch doesn't have any impact on how we pack weights
  // so we pass it as 1
  // Input image height/width also don't have any impact on how we pack
  // weights so we can pass any values
  const fbgemm::conv_param_t<kSpatialDim> conv_p =
      at::native::fbgemm_utils::MakeFbgemmConvParam<kSpatialDim>(
          1, // dummy batch size
          input_channels_per_group * groups, // input channels
          output_channels,
          kSpatialDim == 2 ? std::vector<int>{28, 28} // dummy image size
                           : std::vector<int>{28, 28, 28},
          groups,
          kSpatialDim == 2 ? std::vector<int>{kernel_h, kernel_w}
                           : std::vector<int>{kernel_d, kernel_h, kernel_w},
          std::vector<int>(stride.begin(), stride.end()),
          std::vector<int>(padding.begin(), padding.end()),
          std::vector<int>(dilation.begin(), dilation.end()));

  const auto qtype = weight.qscheme();
  std::vector<int32_t> zero_points;
  if (qtype == c10::kPerTensorAffine) {
    zero_points = {static_cast<int32_t>(weight.q_zero_point())};
  } else if (qtype == c10::kPerChannelAffine) {
    int64_t axis = weight.q_per_channel_axis();
    TORCH_CHECK(
        axis == 0,
        "Only per output channel quantization is supported for the weights");
    zero_points.resize(output_channels);
    for (int i = 0; i < output_channels; ++i) {
      zero_points[i] = weight.q_per_channel_zero_points()[i].item<int32_t>();
    }
  } else {
    TORCH_CHECK(false, "Unsupported qscheme: ", toString(qtype));
  }

  // FBGEMM expects weights to be in channels last
  // TODO: Change this when ChannelsLast3d is ready.
  const at::Tensor weight_nhwc = kSpatialDim == 2
      ? weight.contiguous(c10::MemoryFormat::ChannelsLast)
      : at::native::fbgemm_utils::ConvertToChannelsLast3dTensor(weight);
  const int8_t* weight_data_int8 =
      reinterpret_cast<int8_t*>(weight_nhwc.data_ptr<c10::qint8>());
  std::vector<int32_t> col_offsets(output_channels);
  // compute column offsets (Similar to
  // fbgemm::col_offsets_with_zero_pt_s8acc32_ref) please note that offsets
  // include the sum of columns as well as the scalar term weight_zero_point *
  // KDim
  const int output_channels_per_group = output_channels / groups;
  const int inner_size =
      kernel_d * kernel_h * kernel_w * input_channels_per_group;
  for (int g = 0; g < groups; ++g) {
    for (int i = 0; i < output_channels_per_group; ++i) {
      const int c = g * output_channels_per_group + i;
      int32_t sum = 0;
      for (int j = 0; j < inner_size; ++j) {
        sum += static_cast<int32_t>(weight_data_int8[c * inner_size + j]);
      }
      if (qtype == c10::kPerTensorAffine) {
        col_offsets[c] = sum - zero_points[0] * inner_size;
      } else {
        col_offsets[c] = sum - zero_points[c] * inner_size;
      }
    }
  }

  std::vector<float> scales;
  if (qtype == c10::kPerTensorAffine) {
    scales = {static_cast<float>(weight.q_scale())};
  } else if (qtype == c10::kPerChannelAffine) {
    scales.resize(output_channels);
    for (int i = 0; i < output_channels; ++i) {
      scales[i] = weight.q_per_channel_scales()[i].item<float>();
    }
  }

  c10::optional<at::Tensor> bias_contig;
  if (bias.has_value()) {
    at::Tensor bias_vec = bias.value();
    TORCH_CHECK(bias_vec.dim() == 1, "bias should be a vector (1D Tensor)");
    TORCH_CHECK(
        bias_vec.size(0) == output_channels,
        "bias should have K elements: " + std::to_string(output_channels));
    bias_contig = bias->contiguous();
  }

  auto ret_ptr = c10::make_intrusive<PackedConvWeight<kSpatialDim>>(
      PackedConvWeight<kSpatialDim>{
          std::make_unique<fbgemm::PackWeightsForConv<kSpatialDim>>(
              conv_p, weight_data_int8),
          bias_contig,
          stride,
          padding,
          dilation,
          groups,
          col_offsets,
          kSpatialDim == 2 ? std::vector<int64_t>{kernel_h, kernel_w}
                           : std::vector<int64_t>{kernel_d, kernel_h, kernel_w},
          scales,
          zero_points,
          qtype});

  return ret_ptr;
}

#endif // USE_FBGEMM

#ifdef USE_PYTORCH_QNNPACK
template <int kSpatialDim>
c10::intrusive_ptr<ConvPackedParamsBase<kSpatialDim>> PackedConvWeightsQnnp<
    kSpatialDim>::
    prepack(
        at::Tensor weight,
        c10::optional<at::Tensor> bias_in,
        torch::List<int64_t> stride,
        torch::List<int64_t> padding,
        torch::List<int64_t> dilation,
        int64_t groups) {
  TORCH_CHECK(
      weight.ndimension() == 4,
      "quantized::conv2d_prepack (qnnpack): Weights are expected to have 4 "
      "dimensions");
  TORCH_CHECK(
      stride.size() == 2,
      "quantized::conv2d_prepack (qnnpack): 2D convolution only");
  TORCH_CHECK(
      padding.size() == 2,
      "quantized::conv2d_prepack (qnnpack): Specify top/left padding only. "
      "bottom/right padding assumed to be equal to top/left");
  TORCH_CHECK(
      dilation.size() == 2,
      " quantized::conv2d_prepack (qnnpack): 2D convolution only");

  at::native::initQNNPACK();

  // QNNPACK expects weights to be of the format {out_c, kH, kW, in_c/groups},
  // but PyTorch lays them out as {out_c, in_c/groups, kH, kW}
  const size_t out_ch = weight.size(0);
  const uint32_t kernel_h = weight.size(2);
  const uint32_t kernel_w = weight.size(3);

  at::Tensor bias_fp32;
  if (bias_in.has_value()) {
    bias_fp32 = bias_in.value();
  } else {
    bias_fp32 = at::zeros(out_ch, weight.options().dtype(at::kFloat));
  }

  TORCH_CHECK(
      !bias_fp32.defined() ||
          (bias_fp32.ndimension() == 1 && bias_fp32.size(0) == out_ch),
      "quantized::conv2d_prepack (qnnpack): expected bias to be 1-dimensional "
      "with ",
      out_ch,
      " elements",
      ", but got bias of size ",
      bias_fp32.sizes(),
      " instead");

  auto weight_contig = weight.contiguous(c10::MemoryFormat::ChannelsLast);
  const bool is_per_channel = weight_contig.qscheme() == at::kPerChannelAffine;

  std::vector<uint8_t> w_zero_points;
  at::Tensor  w_scales;
  std::tie(w_zero_points, w_scales) =
      make_zero_points_and_scales_tensor(weight_contig);
  // We set the pre-packed conv weights to nullptr below as we call pre-pack
  // during the first invocation of operator run. Refer to qconv.cpp for more
  // details. TODO Update to actually call pre-pack here once bias is removed
  // from pre-packing step.
  c10::intrusive_ptr<ConvPackedParamsBase<kSpatialDim>> ret_ptr =
      c10::make_intrusive<PackedConvWeightsQnnp<kSpatialDim>>(
          PackedConvWeightsQnnp<kSpatialDim>{
              nullptr, /* PrePackConvWeights */
              weight_contig, /* int8_t weight */
              bias_fp32.contiguous(), /* fp32 bias */
              stride,
              padding,
              dilation,
              groups,
              c10::nullopt, /* input_scale */
              {kernel_h, kernel_w},
              w_scales,
              std::move(w_zero_points),
              is_per_channel});

  return ret_ptr;
}

#endif // USE_PYTORCH_QNNPACK

namespace at {
namespace native {
namespace {

template <int kSpatialDim = 2>
class QConvPackWeightInt8 final {
 public:
  static c10::intrusive_ptr<ConvPackedParamsBase<kSpatialDim>> run(
      Tensor weight,
      c10::optional<Tensor> bias,
      torch::List<int64_t> stride,
      torch::List<int64_t> padding,
      torch::List<int64_t> dilation,
      int64_t groups) {
    auto& ctx = at::globalContext();
#ifdef USE_FBGEMM
    if (ctx.qEngine() == at::QEngine::FBGEMM) {
      return PackedConvWeight<kSpatialDim>::prepack(
          weight, bias, stride, padding, dilation, groups);
    }
#endif

#ifdef USE_PYTORCH_QNNPACK
    if (ctx.qEngine() == at::QEngine::QNNPACK) {
      TORCH_CHECK(
          kSpatialDim == 2,
          "quantized::conv_prepack (qnnpack): QNNPACK only supports Conv1d "
          "and Conv2d now.");
      return PackedConvWeightsQnnp<kSpatialDim>::prepack(
          weight, bias, stride, padding, dilation, groups);
    }
#endif

    TORCH_CHECK(
        false,
        "Didn't find engine for operation quantized::conv2d_prepack ",
        toString(ctx.qEngine()));
  }
};

class QConv1dPackWeightInt8 final {
 public:
  static c10::intrusive_ptr<ConvPackedParamsBase<2>> run(
      Tensor weight,
      c10::optional<Tensor> bias,
      torch::List<int64_t> stride,
      torch::List<int64_t> padding,
      torch::List<int64_t> dilation,
      int64_t groups) {
    auto& ctx = at::globalContext();
    if (weight.dim() == 3) {
      weight = weight.unsqueeze(quant_utils::kConv1dSqueezeDim + 2);
    }
    stride = quant_utils::MakeArgForConv1d(stride, 1);
    padding = quant_utils::MakeArgForConv1d(padding, 0);
    dilation = quant_utils::MakeArgForConv1d(dilation, 1);
#ifdef USE_FBGEMM
    if (ctx.qEngine() == at::QEngine::FBGEMM) {
      return PackedConvWeight<2>::prepack(
          weight, bias, stride, padding, dilation, groups);
    }
#endif

#ifdef USE_PYTORCH_QNNPACK
    if (ctx.qEngine() == at::QEngine::QNNPACK) {
      return PackedConvWeightsQnnp<2>::prepack(
          weight, bias, stride, padding, dilation, groups);
    }
#endif
    TORCH_CHECK(
        false,
        "Didn't find engine for operation quantized::conv1d_prepack ",
        toString(ctx.qEngine()));
  }
};

TORCH_LIBRARY_IMPL(quantized, QuantizedCPU, m) {
  // conv_prepack is deprecated, please use conv2d_prepack for 2D conv.
  m.impl("conv_prepack", TORCH_FN(QConvPackWeightInt8<2>::run));
  m.impl("conv1d_prepack", TORCH_FN(QConv1dPackWeightInt8::run));
  m.impl("conv2d_prepack", TORCH_FN(QConvPackWeightInt8<2>::run));
  m.impl("conv3d_prepack", TORCH_FN(QConvPackWeightInt8<3>::run));
}

TORCH_LIBRARY_IMPL(_quantized, QuantizedCPU, m) {
  m.impl("conv2d_prepack", TORCH_FN(QConvPackWeightInt8<2>::run));
}

} // namespace
} // namespace native
} // namespace at
