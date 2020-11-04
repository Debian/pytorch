#include "cpp_c10d_extension.hpp"

#include <map>

namespace c10d {

ProcessGroupTest::WorkTest::~WorkTest() {}

bool ProcessGroupTest::WorkTest::isCompleted() {
  return true;
}

bool ProcessGroupTest::WorkTest::isSuccess() const {
  return true;
}

bool ProcessGroupTest::WorkTest::wait(std::chrono::milliseconds /* unused */) {
  return true;
}

ProcessGroupTest::ProcessGroupTest(int rank, int size)
    : ProcessGroup(rank, size) {}

ProcessGroupTest::~ProcessGroupTest() {}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::broadcast(
    std::vector<at::Tensor>& tensors,
    const BroadcastOptions& opts) {
  return std::make_shared<ProcessGroupTest::WorkTest>();
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::allreduce(
    std::vector<at::Tensor>& tensors,
    const AllreduceOptions& opts) {
  return std::make_shared<ProcessGroupTest::WorkTest>();
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::allreduce_coalesced(
      std::vector<at::Tensor>& tensors,
      const AllreduceCoalescedOptions& opts) {
  throw std::runtime_error("ProcessGroupTest does not support allreduce_coalesced");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::reduce(
    std::vector<at::Tensor>& tensors,
    const ReduceOptions& opts) {
  throw std::runtime_error("ProcessGroupTest does not support reduce");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::allgather(
    std::vector<std::vector<at::Tensor>>& outputTensors,
    std::vector<at::Tensor>& inputTensors,
    const AllgatherOptions& opts) {
  throw std::runtime_error("ProcessGroupTest does not support allgather");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::allgather_base(
    at::Tensor& outputBuffer,
    at::Tensor& inputBuffer,
    const AllgatherOptions& opts) {
  throw std::runtime_error("ProcessGroupTest does not support allgather_base");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::barrier(
    const BarrierOptions& opts) {
  return std::make_shared<ProcessGroupTest::WorkTest>();
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::gather(
    std::vector<std::vector<at::Tensor>>& outputTensors,
    std::vector<at::Tensor>& inputTensors,
    const GatherOptions& opts) {
  throw std::runtime_error("ProcessGroupTest does not support gather");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::scatter(
    std::vector<at::Tensor>& outputTensors,
    std::vector<std::vector<at::Tensor>>& inputTensors,
    const ScatterOptions& opts) {
  throw std::runtime_error("ProcessGroupTest does not support scatter");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::reduce_scatter(
    std::vector<at::Tensor>& outputTensors,
    std::vector<std::vector<at::Tensor>>& inputTensors,
    const ReduceScatterOptions& opts) {
  throw std::runtime_error("ProcessGroupTest does not support reduce_scatter");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::send(
    std::vector<at::Tensor>& tensors,
    int dstRank,
    int tag) {
  throw std::runtime_error("ProcessGroupTest does not support send");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::recv(
    std::vector<at::Tensor>& tensors,
    int srcRank,
    int tag) {
  throw std::runtime_error("ProcessGroupTest does not support recv");
}

std::shared_ptr<ProcessGroup::Work> ProcessGroupTest::recvAnysource(
    std::vector<at::Tensor>& tensor,
    int tag) {
  throw std::runtime_error("ProcessGroupTest does not support recvAnysource");
}

std::shared_ptr<ProcessGroup> ProcessGroupTest::createProcessGroupTest(
    const std::shared_ptr<::c10d::Store>& store,
    int rank,
    int size,
    const std::chrono::duration<float>& timeout) {
  return std::make_shared<ProcessGroupTest>(rank, size);
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("createProcessGroupTest", &ProcessGroupTest::createProcessGroupTest);
}

} // namespace c10d
