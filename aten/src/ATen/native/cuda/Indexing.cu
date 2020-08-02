#include <ATen/native/TensorAdvancedIndexing.h>
#include <ATen/native/IndexingUtils.h>

#include <ATen/ATen.h>
#include <ATen/NativeFunctions.h>
#include <ATen/ExpandUtils.h>
#include <ATen/native/TensorIterator.h>
#include <ATen/AccumulateType.h>
#include <ATen/cuda/detail/IndexUtils.cuh>

#include <THC/THCDeviceUtils.cuh>
#include <THC/THCGeneral.h>
#include <THC/THCTensorSort.cuh>
#include <ATen/cuda/CUDAContext.h>
#include <THC/THCThrustAllocator.cuh>
#include <thrust/execution_policy.h>
#include <thrust/sort.h>
#include <THC/THCAtomics.cuh>

#include <c10/macros/Macros.h>

namespace {

template <typename scalar_t, int SZ>
__global__ void indexing_backward_kernel(
  int64_t* sorted_indices, int64_t* indices, scalar_t* grad_output, scalar_t* grad_weight,
  int64_t numel, int64_t stride, int64_t stride_before, int64_t outer_dim) {
//numel is total number of flattened indices, not expanded to dimensions that are not indexed.
//stride is the cumulative size of the not-indexed last dimensions
//stride_before is the stride of the dimension immediately preceding first indexed dimension
//if indexing starts from the 0th dimension, stride_before does not matter because blockIdx.z will be 0 in this case
//outer_dim is number of elements in the first unindexed dimensions
  using accscalar_t = at::acc_type<scalar_t, true>;

  // Each warp is responsible for an input into the LookupTable.
  // If the preceding input has the same destination index as this input, then the warp
  // exits immediately. The warp also processes subsequent inputs with the
  // same value.
  //
  // Input Warp
  // 1     <warp 1>
  // 1     <warp 1> (<warp 2> exits without doing any work)
  // 5     <warp 3>
  // 8     <warp 4>

  // Number of values processed by each thread (grain size)
  for (int64_t z = blockIdx.z; z < outer_dim; z += gridDim.z){
    int64_t idx = blockIdx.x * blockDim.y + threadIdx.y;
    if (idx < numel
        && (idx == 0 || sorted_indices[idx] != sorted_indices[idx - 1])){
      do {
        int64_t start_feature = threadIdx.x + blockIdx.y * blockDim.x * SZ;
        const int64_t weight_row = ((int64_t) sorted_indices[idx]) * stride + z * stride_before;
        const int64_t grad_row = ((int64_t) indices[idx]) * stride + z * numel * stride;
        const accscalar_t scale = (accscalar_t)1.0;

        accscalar_t gradient[SZ];
        accscalar_t weight[SZ];

        while (start_feature < stride) {
          #pragma unroll
          for (int ii = 0; ii < SZ; ii++) {
            int64_t feature_dim = start_feature + ii * C10_WARP_SIZE;
            if (feature_dim < stride) {
              gradient[ii] = static_cast<accscalar_t>(grad_output[grad_row + feature_dim]);
              weight[ii] = static_cast<accscalar_t>(grad_weight[weight_row + feature_dim]);
            }
          }

          #pragma unroll
          for (int ii = 0; ii < SZ; ii++) {
            weight[ii] += gradient[ii] * scale;
          }

          #pragma unroll
          for (int ii = 0; ii < SZ; ii++) {
            int64_t feature_dim = start_feature + ii * C10_WARP_SIZE;
            if (feature_dim < stride) {
                grad_weight[weight_row + feature_dim] = static_cast<scalar_t>(weight[ii]);
            }
          }
          start_feature += gridDim.y * blockDim.x * SZ;
        }

        idx++;
      } while (idx < numel && sorted_indices[idx] == sorted_indices[idx - 1]);
    }
  }
}


}


namespace at { namespace native {

static Tensor wrapIndexOnce(const Tensor & index, int64_t dim, int64_t dim_size, bool check_range=true) {
//we don't need to check range in backward - if there were out of bounds indices forward should already have errored out
  if (index.numel() != 0 && check_range) {
    auto max_idx = index.max().item<int64_t>();
    auto min_idx = index.min().item<int64_t>();
    if (max_idx >= dim_size) {
      TORCH_CHECK_INDEX(false, "index ", max_idx, " is out of bounds for dimension ", dim, " with size ", dim_size);
    }
    if (min_idx < -dim_size) {
      TORCH_CHECK_INDEX(false, "index ", min_idx, " is out of bounds for dimension ", dim, " with size ", dim_size);
    }
  }
  return index.remainder(dim_size);
}

static std::vector<int64_t> computeLinearStride(const Tensor & tensor) {
  // computes the stride as if tensor were contiguous
  auto sizes = tensor.sizes();
  std::vector<int64_t> stride(tensor.dim());
  stride[tensor.dim() - 1] = 1;
  std::partial_sum(sizes.rbegin(), sizes.rend() - 1, stride.rbegin() + 1, std::multiplies<int64_t>());
  return stride;
}

static std::tuple<Tensor, int64_t, int64_t, int64_t>
computeLinearIndex(const Tensor & src, TensorList indices, bool check_range) {
  auto strides = computeLinearStride(src);
  const auto& backend = src.type().backend();

  // Compute the linear index by multiplying the indexing tensors by the
  // stride and summing them. All the indexing tensors have the same shape at
  // this point. We also compute the number of dimensions before and after that
  // are not being index.
  Tensor linearIndex;
  int64_t emptyBefore = 0, emptyAfter = 0, nElemBefore = 1, nElemAfter = 1, strideBefore =0;
  for (auto i = decltype(src.dim()){0}; i < src.dim(); i++) {
    if (indices[i].defined()) {
      // Cast index to the longType matching src's backend
      // This allows us to support ie indexing a cuda tensor with a cpu tensor
      Tensor index = (wrapIndexOnce(indices[i], i, src.size(i), check_range) * strides[i]).toBackend(backend);
      if (linearIndex.defined()) {
        linearIndex += index;
      } else {
        linearIndex = index;
        if (i>0) {
           strideBefore = src.stride(i-1); // stride after undefined dimensions
        }
      }
    } else if (linearIndex.defined()) {
      emptyAfter++;
      nElemAfter *= src.size(i);
    } else {
      emptyBefore++;
      nElemBefore *= src.size(i);
    }
  }

  return std::make_tuple(std::move(linearIndex), nElemBefore, strideBefore, nElemAfter);
}


static std::tuple<Tensor, Tensor, int64_t, int64_t, int64_t, std::vector<int64_t>> makeLinearIndex(Tensor self, TensorList orig, bool check_range) {
  checkIndexTensorTypes(orig);
  // first expand BoolTensor (masks) or ByteTensor (masks) into 1 or more LongTensors
  auto indices = expandTensors(self, orig);
  // next broadcast all index tensors together
  indices = expand_outplace(indices);
  // add missing null Tensors so that it matches self.dim()
  while (indices.size() < (size_t)self.dim()) {
    indices.emplace_back();
  }
  // if the non-null indices are not all adjacent, transpose self and indices
  // together so that they're adjacent at the front
  std::vector<int64_t> inversePerm;
  if (!hasContiguousSubspace(indices)) {
    std::tie(self, indices, inversePerm) = transposeToFrontAndInvPerm(self, indices);
  }
  int64_t nElemBefore, strideBefore, nElemAfter;
  Tensor linearIndex;
  std::tie(linearIndex, nElemBefore, strideBefore, nElemAfter) = computeLinearIndex(self, indices, check_range);
  return std::make_tuple(linearIndex, self, nElemBefore, strideBefore, nElemAfter, inversePerm);
}


namespace {
void index_put_accum_kernel(Tensor & self, TensorList indices, const Tensor & value, bool unsafe) {
  if (indices.size() > (size_t)self.dim()) {
    TORCH_CHECK_INDEX(false, "too many indices for tensor of dimension ", self.dim(), " (got ", indices.size(), ")");
  }
  auto value_ = value.contiguous();
  Tensor linearIndex, expandedValue, src;
  int64_t nElemBefore, strideBefore, sliceSize;
  std::vector<int64_t> inversePerm;
  std::tie(linearIndex, src, nElemBefore, strideBefore, sliceSize, inversePerm) = makeLinearIndex(self, indices, !unsafe);
  int64_t num_indices = linearIndex.numel();
  if (num_indices > 0 && sliceSize > 0) {
      const bool permuted = !src.is_contiguous();
      auto src_ = permuted ? src.contiguous() : src;
      linearIndex = linearIndex.reshape(-1);
      auto sorted_indices = at::empty_like(linearIndex, LEGACY_CONTIGUOUS_MEMORY_FORMAT);
      auto orig_indices = at::empty_like(linearIndex, LEGACY_CONTIGUOUS_MEMORY_FORMAT);
      using device_ptr = thrust::device_ptr<int64_t>;
      const cudaStream_t stream = at::cuda::getCurrentCUDAStream();

      linearIndex.floor_divide_(sliceSize);
      {
      sorted_indices.copy_(linearIndex);
      auto allocator = THCThrustAllocator(globalContext().lazyInitCUDA());
      auto policy = thrust::cuda::par(allocator).on(stream);

      // Fill sortedOrigIndices with sequential indices
      const auto count_iter = thrust::counting_iterator<int64_t>(0);
      auto orig_data = device_ptr(orig_indices.data_ptr<int64_t>());
      thrust::copy(policy, count_iter, count_iter + num_indices, orig_data);

      // Sort the inputs into sorted with the corresponding indices; we
      // don't need a stable or multidimensional sort, so just use Thrust
      // directly
      // Sort; a stable sort is not required
      // NB - not passing comparator causes thrust to use radix sort, and it hurts perf A LOT, at least for medium (few K) sized indices
      auto sorted_data = device_ptr(sorted_indices.data_ptr<int64_t>());
      thrust::sort_by_key(policy, sorted_data, sorted_data + num_indices, orig_data, ThrustLTOp<int64_t>());
      }
      TORCH_INTERNAL_ASSERT(linearIndex.numel()*sliceSize*nElemBefore == value.numel(), "number of flattened indices did not match number of elements in the value tensor", linearIndex.numel()*sliceSize*nElemBefore, value.numel());
      const int UNROLL = 4;
      const int indices_per_block = 4;
      dim3 grid(THCCeilDiv(num_indices, (int64_t) indices_per_block),
           std::min<int>(at::cuda::getCurrentDeviceProperties()->maxGridSize[1], THCCeilDiv(sliceSize, (int64_t) (C10_WARP_SIZE*UNROLL))),
           std::min(std::max<int>(1,nElemBefore), at::cuda::getCurrentDeviceProperties()->maxGridSize[2]));
      dim3 block(C10_WARP_SIZE, indices_per_block);

      AT_DISPATCH_ALL_TYPES_AND3(at::ScalarType::Half, at::ScalarType::Bool, at::ScalarType::BFloat16,
      value_.scalar_type(), "indexing_backward", [&] {
      AT_SKIP_BFLOAT16_IF_NOT_ROCM(scalar_t, "indexing_backward", [&] {
      indexing_backward_kernel<scalar_t, UNROLL><<<grid, block, 0, stream>>>(
        sorted_indices.data_ptr<int64_t>(),
        orig_indices.data_ptr<int64_t>(),
        value_.data_ptr<scalar_t>(),
        src_.data_ptr<scalar_t>(),
        num_indices,
        sliceSize,
        strideBefore,
        nElemBefore);
      });
      });
      AT_CUDA_CHECK(cudaGetLastError());
      if (permuted)
          self.copy_(src_.permute(inversePerm));
  }
}

REGISTER_CUDA_DISPATCH(index_put_accum_stub, &index_put_accum_kernel);
} //anonymous


// Check tensor dimensions for index operations, and return the slice size.
static ptrdiff_t getSliceSize(const Tensor & dst,
                              int dim,
                              const Tensor & index,
                              const Tensor & src)
{
  int dstDims = dst.dim();
  int srcDims = src.dim();

  TORCH_CHECK(index.dim() <= 1, "Index must be vector or scalar");

  ptrdiff_t dstSliceSize = 1;
  TORCH_CHECK(dim >= 0 && dim < dstDims, "Indexing dim ", dim, " is out of bounds");
  for (int d = 0; d < dstDims; d++) {
    if (d != dim) {
      dstSliceSize *= dst.size(d);
    }
  }

  TORCH_CHECK(dim < srcDims, "Indexing dim ", dim, " is out of bounds");
  TORCH_CHECK(index.numel() == src.size(dim),
             "length of src.size[dim] is not equal to length of indices");

  ptrdiff_t srcSliceSize = 1;
  bool mismatch = false;

  if (dstDims != srcDims) mismatch = true;

  for (int d = 0; d < srcDims; d++) {
    if (d != dim) {
      srcSliceSize *= src.size(d);
      if (!mismatch && dst.size(d) != src.size(d)) mismatch = true;
    }
  }

  TORCH_CHECK(dstSliceSize == srcSliceSize,
             "Source/destination tensor have different slice sizes (%ld vs %ld)",
             dstSliceSize, srcSliceSize);

  if (mismatch) {
    TORCH_WARN_ONCE(
        "Warning: source/destination slices have same size but different "
        "shape for an index operation.  This behavior is deprecated.\n");
  }

  return dstSliceSize;
}

// We prefer this kernel to avoid reloading index points if the number
// of indices is a small number.
// This kernel in fact works for all choices of problem size, but if
// the number of indices chosen is large, then the
// indexAddLargeIndex kernel is a better choice to increase
// parallelism.
template <typename T, typename IndexType, int DstDim, int SrcDim, int IdxDim>
__global__ void indexAddSmallIndex(cuda::detail::TensorInfo<T, IndexType> dst,
                                   cuda::detail::TensorInfo<T, IndexType> src,
                                   cuda::detail::TensorInfo<int64_t, IndexType> indices,
                                   int dstAddDim,
                                   int srcAddDim,
                                   IndexType innerSize,
                                   int64_t dstAddDimSize) {
  // In order to avoid reloading the index that we are copying, load
  // it once to handle all of the points that are being selected, so
  // it can be reused as much as possible. This kernel is chosen when
  // this is a good choice (small number of chosen indices), since
  // re-accessing indices in addition to src elements can be slow.
  for (IndexType srcIndex = 0; srcIndex < indices.sizes[0]; ++srcIndex) {
    // Lua indices begin at 1
    IndexType dstIndex =
        indices.data[cuda::detail::IndexToOffset<int64_t, IndexType, IdxDim>::get(srcIndex, indices)];
    CUDA_KERNEL_ASSERT(dstIndex < dstAddDimSize);

    // We stride over the output ignoring the indexed dimension
    // (innerSize), whose offset calculation is handled differently
    for (IndexType linearIndex = blockIdx.x * blockDim.x + threadIdx.x;
         linearIndex < innerSize;
         linearIndex += gridDim.x * blockDim.x) {
      IndexType dstOffset =
          cuda::detail::IndexToOffset<T, IndexType, DstDim>::get(linearIndex, dst);
      dstOffset += dstIndex * dst.strides[dstAddDim];

      IndexType srcOffset =
          cuda::detail::IndexToOffset<T, IndexType, SrcDim>::get(linearIndex, src);
      srcOffset += srcIndex * src.strides[srcAddDim];

      gpuAtomicAdd(&dst.data[dstOffset], src.data[srcOffset]);
    }
  }
}

// We prefer this kernel to balance parallelism across index points,
// if there are a large number of indices.
// This kernel in fact works for all choices of problem size, but if
// the number of indices chosen is small, then the
// indexAddSmallIndex kernel is a better choice to reduce memory
// accesses.
template <typename T, typename IndexType, int DstDim, int SrcDim, int IdxDim,
          bool IndexIsMajor>
__global__ void indexAddLargeIndex(cuda::detail::TensorInfo<T, IndexType> dst,
                                   cuda::detail::TensorInfo<T, IndexType> src,
                                   cuda::detail::TensorInfo<int64_t, IndexType> indices,
                                   int dstAddDim,
                                   int srcAddDim,
                                   IndexType totalSize,
                                   IndexType innerSize,
                                   int64_t dstAddDimSize) {
  // We stride over the output including the indexed dimension
  // (totalSize), and calculate the destination index point based on that
  for (IndexType linearIndex = blockIdx.x * blockDim.x + threadIdx.x;
       linearIndex < totalSize;
       linearIndex += gridDim.x * blockDim.x) {
    IndexType srcIndex, elementInSlice;
    if (IndexIsMajor) {
      srcIndex = linearIndex / innerSize;
      elementInSlice = linearIndex % innerSize;
    }
    else {
      elementInSlice = linearIndex / innerSize;
      srcIndex = linearIndex % innerSize;
    }

    // Lua indices begin at 1
    IndexType dstIndex =
        indices.data[cuda::detail::IndexToOffset<int64_t, IndexType, IdxDim>::get(srcIndex, indices)];
    CUDA_KERNEL_ASSERT(dstIndex < dstAddDimSize);

    IndexType dstOffset =
      cuda::detail::IndexToOffset<T, IndexType, DstDim>::get(elementInSlice, dst);
    dstOffset += dstIndex * dst.strides[dstAddDim];

    IndexType srcOffset =
      cuda::detail::IndexToOffset<T, IndexType, SrcDim>::get(elementInSlice, src);
    srcOffset += srcIndex * src.strides[srcAddDim];

    gpuAtomicAdd(&dst.data[dstOffset], src.data[srcOffset]);
  }
}

// Compare the stride between adjacent slices (sliceStride) with strides in the
// other dimensions (i.e., strides *inside* each slice).
//
// - Returns true if some dimension inside the slice has lower stride than
//   sliceStride.  The simplest example is a 2-D contiguous tensor with sliceDim
//   == 0 (that is, each slice is a row).
//
//   In this case, we choose the CUDA kernel that processes the data in
//   "index-major order".  For example, if thread count equals slice size, then
//   all threads process slice #0 in lockstep, and then slice #1, and so on.
//
// - Otherwise (i.e., sliceStride has the lowest value), this function returns
//   false.  The simplest example is a 2-D contiguous tensor with sliceDim == 1
//   (each slice is a column).
//
//   In this case, we choose the CUDA kernel that processes the data in
//   "elementInSlice-major order".  For example, each thread can process element
//   #0 of every slice, and then element #1 of every slice, and so on.
template <typename scalar_t>
bool indexShouldBeMajor(cuda::detail::TensorInfo<scalar_t, unsigned int> &info,
                                    int sliceDim)
{
  // The stride between adjacent slices (e.g., between element #0 of slice #100
  // and element #0 of slice #101).
  unsigned int sliceStride = info.strides[sliceDim];

  for (int i = 0; i < info.dims; ++i) {
    if (i != sliceDim && info.sizes[i] > 1 && info.strides[i] < sliceStride) {
      return true;
    }
  }

  return false;
}

Tensor& index_add_cuda_(Tensor & self, int64_t dim, const Tensor & index, const Tensor & source) {
  dim = maybe_wrap_dim(dim, self.dim());

  TensorArg self_arg{self, "self", 1}, index_arg{index, "index", 3}, source_arg{source, "source", 4};
  checkAllSameGPU("index_add", {self_arg, index_arg, source_arg});

  TORCH_CHECK_INDEX(index.dim() <= 1, "index_add_(): Index is supposed to be a vector");
  TORCH_CHECK(index.scalar_type() == ScalarType::Long, "index_add_(): Expected dtype int64 for index");
  TORCH_CHECK(self.scalar_type() == source.scalar_type(),
              "index_add_(): self and source must have the same scalar type");
  TORCH_CHECK(dim == 0 || dim < source.dim(),
              "index_add_(): Indexing dim ", dim, " is out of bounds of tensor");
  TORCH_CHECK(index.numel() == (source.dim() == 0 ? 1 : source.size(dim)),
              "index_add_(): Number of indices should be equal to self.size(dim)");

  // Scalars are treated as 1-d tensor
  Tensor self_ = (self.dim() == 0) ? self.view(1) : self;
  Tensor source_ = (source.dim() == 0) ? source.view(1) : source;

  TORCH_CHECK(self.dim() <= MAX_CUTORCH_DIMS, CUTORCH_DIM_WARNING);
  TORCH_CHECK(source.dim() <= MAX_CUTORCH_DIMS, CUTORCH_DIM_WARNING);
  TORCH_CHECK(index.dim() <= MAX_CUTORCH_DIMS, CUTORCH_DIM_WARNING);

  // The `source` is partitioned into two parts:
  // -the size of each slice we are indexing, which is the
  // total size of the tensor ignoring dimension `dim`;
  // -the number of index we are choosing, which is the total size
  // of the tensor `index`.
  ptrdiff_t sliceSize = getSliceSize(self_, dim, index, source_);
  ptrdiff_t sourceTotalSize = source.numel();
  int64_t selfAddDimSize = self_.size(dim);
  ptrdiff_t numIndex = index.numel();

  if (sliceSize == 0) {
    return self;
  }
  const cudaStream_t stream = at::cuda::getCurrentCUDAStream();
  bool indContig = index.is_contiguous();

  int mpc = at::cuda::getCurrentDeviceProperties()->multiProcessorCount;

#define SMALL_INDEX(TENSOR_TYPE, TYPE, SELF_DIM, SOURCE_DIM, IDX_DIM) \
  indexAddSmallIndex<TENSOR_TYPE, TYPE, SELF_DIM, SOURCE_DIM, IDX_DIM> \
    <<<smallIndexGrid, smallIndexBlock, 0, stream>>>(   \
      selfInfo, sourceInfo, indexInfo,                    \
      selfAddDim, sourceAddDim, sliceSize, selfAddDimSize);

#define LARGE_INDEX(TENSOR_TYPE, TYPE,                        \
                    SELF_DIM, SOURCE_DIM, IDX_DIM, IDX_IS_MAJOR)  \
  indexAddLargeIndex<TENSOR_TYPE, TYPE,                       \
                     SELF_DIM, SOURCE_DIM, IDX_DIM, IDX_IS_MAJOR> \
    <<<largeIndexGrid, largeIndexBlock, 0, stream>>>(         \
      selfInfo, sourceInfo, indexInfo,                          \
      selfAddDim, sourceAddDim, sourceTotalSize,                     \
      (IDX_IS_MAJOR) ? sliceSize : numIndex,                \
      selfAddDimSize);

  dim3 smallIndexGrid(std::min(THCCeilDiv(sliceSize, (ptrdiff_t)128), (ptrdiff_t)(mpc * 8)));
  dim3 smallIndexBlock(std::min(sliceSize, (ptrdiff_t)128));

  dim3 largeIndexGrid(std::min(THCCeilDiv(sourceTotalSize, (ptrdiff_t)128), (ptrdiff_t)(mpc * 8)));
  dim3 largeIndexBlock(std::min(sourceTotalSize, (ptrdiff_t)128));

  if (cuda::detail::canUse32BitIndexMath(self) &&
      cuda::detail::canUse32BitIndexMath(source) &&
      cuda::detail::canUse32BitIndexMath(index)) {
    AT_DISPATCH_ALL_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16, self.scalar_type(), "index_add", [&] {
      AT_SKIP_BFLOAT16_IF_NOT_ROCM(scalar_t, "index_add", [&] {
        cuda::detail::TensorInfo<scalar_t, unsigned int> selfInfo =
            cuda::detail::getTensorInfo<scalar_t, unsigned int>(self_);
        int selfAddDim = selfInfo.collapseDims(dim);
        selfInfo.reduceDim(selfAddDim);

        auto sourceInfo =
          cuda::detail::getTensorInfo<scalar_t, unsigned int>(source_);
        int sourceAddDim = sourceInfo.collapseDims(dim);
        sourceInfo.reduceDim(sourceAddDim);

        auto indexInfo =
         cuda::detail::getTensorInfo<int64_t, unsigned int>(index);
        indexInfo.collapseDims();

        // A reasonable choice for when to have each thread iterate over
        // index to choose
        if (numIndex <= 16) {
          if (selfInfo.dims == 1 && sourceInfo.dims == 1 && indContig) {
            SMALL_INDEX(scalar_t, unsigned int, 1, 1, -2);
          } else if (selfInfo.dims == 2 && sourceInfo.dims == 2 && indContig) {
            SMALL_INDEX(scalar_t, unsigned int, 2, 2, -2);
          } else if (selfInfo.dims == 3 && sourceInfo.dims == 3 && indContig) {
            SMALL_INDEX(scalar_t, unsigned int, 3, 3, -2);
          } else {
            SMALL_INDEX(scalar_t, unsigned int, -1, -1, -1);
          }
        } else {
          bool indexIsMajor = indexShouldBeMajor(selfInfo, selfAddDim);

          if (selfInfo.dims == 1 && sourceInfo.dims == 1 && indContig) {
            LARGE_INDEX(scalar_t, unsigned int, 1, 1, -2, true);
          } else if (selfInfo.dims == 2 && sourceInfo.dims == 2 && indContig) {
            if (indexIsMajor) {
              LARGE_INDEX(scalar_t, unsigned int, 2, 2, -2, true);
            } else {
              LARGE_INDEX(scalar_t, unsigned int, 2, 2, -2, false);
            }
          } else if (selfInfo.dims == 3 && sourceInfo.dims == 3 && indContig) {
            if (indexIsMajor) {
              LARGE_INDEX(scalar_t, unsigned int, 3, 3, -2, true);
            } else {
              LARGE_INDEX(scalar_t, unsigned int, 3, 3, -2, false);
            }
          } else {
            LARGE_INDEX(scalar_t, unsigned int, -1, -1, -1, true);
          }
        }
      });
    });
  } else {
    AT_DISPATCH_ALL_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16, self.scalar_type(), "index_add", [&] {
      AT_SKIP_BFLOAT16_IF_NOT_ROCM(scalar_t, "index_add", [&] {
        cuda::detail::TensorInfo<scalar_t, uint64_t> selfInfo =
          cuda::detail::getTensorInfo<scalar_t, uint64_t>(self_);
        int selfAddDim = selfInfo.collapseDims(dim);
        selfInfo.reduceDim(selfAddDim);

        cuda::detail::TensorInfo<scalar_t, uint64_t> sourceInfo =
          cuda::detail::getTensorInfo<scalar_t, uint64_t>(source_);
        int sourceAddDim = sourceInfo.collapseDims(dim);
        sourceInfo.reduceDim(sourceAddDim);

        cuda::detail::TensorInfo<int64_t, uint64_t> indexInfo =
          cuda::detail::getTensorInfo<int64_t, uint64_t>(index);
        indexInfo.collapseDims();

        LARGE_INDEX(scalar_t, uint64_t, -1, -1, -1, true);
      });
    });
  }

  return self;
#undef SMALL_INDEX
#undef LARGE_INDEX
}

} //at
} //native
