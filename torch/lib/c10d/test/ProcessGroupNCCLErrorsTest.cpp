#include <chrono>

#include <c10d/FileStore.hpp>
#include <c10d/ProcessGroupNCCL.hpp>
#include <c10d/test/CUDATest.hpp>
#include <c10d/test/TestUtils.hpp>
#include <torch/csrc/cuda/nccl.h>

#include <gtest/gtest.h>

using namespace c10d::test;

constexpr int kNcclErrorHandlingVersion = 2400;

class WorkNCCLSimulateErrors : public c10d::ProcessGroupNCCL::WorkNCCL {
 public:
  WorkNCCLSimulateErrors(
      const std::vector<at::Device>& devices,
      bool simulate_error)
      : WorkNCCL(devices), simulate_error_(simulate_error) {}

  std::exception_ptr checkForNCCLErrors(
      const std::vector<std::shared_ptr<c10d::NCCLComm>>& ncclComms)
      const override {
    if (simulate_error_) {
      return std::make_exception_ptr(std::runtime_error("Error"));
    }
    return c10d::ProcessGroupNCCL::WorkNCCL::checkForNCCLErrors(ncclComms);
  }

 private:
  bool simulate_error_;
};

class ProcessGroupNCCLSimulateErrors : public c10d::ProcessGroupNCCL {
 public:
  ProcessGroupNCCLSimulateErrors(
      const std::shared_ptr<c10d::Store>& store,
      int rank,
      int size,
      c10d::ProcessGroupNCCL::Options opts)
      : ProcessGroupNCCL(store, rank, size, opts), simulate_error_(false) {}

  std::exception_ptr checkForNCCLErrors(
      const std::vector<std::shared_ptr<c10d::NCCLComm>>& ncclComms) override {
    if (simulate_error_) {
      return std::make_exception_ptr(std::runtime_error("Error"));
    }
    return c10d::ProcessGroupNCCL::checkForNCCLErrors(ncclComms);
  }

  std::chrono::duration<int64_t, std::milli> getWatchdogSleepInterval() {
    return std::chrono::milliseconds(
        ProcessGroupNCCLSimulateErrors::kWatchdogThreadSleepMillis);
  }

  std::shared_ptr<ProcessGroupNCCL::WorkNCCL> initWork(
      std::vector<at::Device> devices) override {
    return std::make_shared<WorkNCCLSimulateErrors>(devices, simulate_error_);
  }

  size_t getNCCLCommCacheSize() {
    return devNCCLCommMap_.size();
  }

  void simulate_error() {
    simulate_error_ = true;
  }

  void reset_error() {
    simulate_error_ = false;
  }

 private:
  bool simulate_error_;
};

class WorkNCCLTimedoutErrors : public c10d::ProcessGroupNCCL::WorkNCCL {
 public:
  WorkNCCLTimedoutErrors(
      const std::vector<at::Device>& devices,
      bool set_timedout_error)
      : WorkNCCL(devices), set_timedout_error_(set_timedout_error) {}

 private:
  bool isCompleted() override {
    if (set_timedout_error_) {
      return false;
    }
    return c10d::ProcessGroupNCCL::WorkNCCL::isCompleted();
  }

 private:
  bool set_timedout_error_;
};

class ProcessGroupNCCLTimedOutErrors : public ProcessGroupNCCLSimulateErrors {
 public:
  ProcessGroupNCCLTimedOutErrors(
      const std::shared_ptr<c10d::Store>& store,
      int rank,
      int size,
      c10d::ProcessGroupNCCL::Options opts)
      : ProcessGroupNCCLSimulateErrors(store, rank, size, opts),
        set_timedout_error_(false) {}

  std::shared_ptr<ProcessGroupNCCL::WorkNCCL> initWork(
      std::vector<at::Device> devices) override {
    return std::make_shared<WorkNCCLTimedoutErrors>(
        devices, set_timedout_error_);
  }

  void set_timedout_error() {
    set_timedout_error_ = true;
  }

  void reset_timedout_error() {
    set_timedout_error_ = false;
  }

 private:
  bool set_timedout_error_;
};

class ProcessGroupNCCLErrorsTest : public ::testing::Test {
 protected:
  bool skipTest() {
    if (cudaNumDevices() == 0) {
      LOG(INFO) << "Skipping test since CUDA is not available";
      return true;
    }
#ifdef USE_C10D_NCCL
    if (torch::cuda::nccl::version() < kNcclErrorHandlingVersion) {
      LOG(INFO) << "Skipping test since NCCL version is too old";
      return true;
    }
#endif
    return false;
  }

  void SetUp() override {
    size_t numDevices = cudaNumDevices();
    TemporaryFile file;
    store_ = std::make_shared<::c10d::FileStore>(file.path, 1);

    at::cuda::OptionalCUDAGuard deviceGuard;
    tensors_.resize(numDevices);
    for (auto i = 0; i < numDevices; ++i) {
      deviceGuard.set_index(i);
      tensors_[i] = at::ones({3, 3}, at::kCUDA);
    }
  }

  void TearDown() override {
    ASSERT_TRUE(setenv(c10d::NCCL_BLOCKING_WAIT, "0", 1) == 0);
  }

  std::vector<at::Tensor> tensors_;
  std::shared_ptr<::c10d::FileStore> store_;
};

TEST_F(ProcessGroupNCCLErrorsTest, testNCCLErrorsBlocking) {
  if (skipTest()) {
    return;
  }

  ASSERT_TRUE(setenv(c10d::NCCL_BLOCKING_WAIT, "1", 1) == 0);
  c10d::ProcessGroupNCCL::Options options;
  options.opTimeout = std::chrono::milliseconds(1000);
  ProcessGroupNCCLSimulateErrors pg(
      store_, 0, 1, options);

  auto work = pg.allreduce(tensors_);
  work->wait();
  EXPECT_TRUE(work->isSuccess());
  EXPECT_EQ(1, pg.getNCCLCommCacheSize());

  // Now run all reduce with errors.
  pg.simulate_error();
  work = pg.allreduce(tensors_);
  EXPECT_THROW(work->wait(), std::runtime_error);

  // Verify the work item failed.
  EXPECT_TRUE(work->isCompleted());
  EXPECT_FALSE(work->isSuccess());
  EXPECT_THROW(work->wait(), std::runtime_error);

  // Communicators might be aborted here, further operations would fail.
}

TEST_F(ProcessGroupNCCLErrorsTest, testNCCLTimedoutErrorsBlocking) {
  if (skipTest()) {
    return;
  }

  ASSERT_TRUE(setenv(c10d::NCCL_BLOCKING_WAIT, "1", 1) == 0);
  c10d::ProcessGroupNCCL::Options options;
  options.opTimeout = std::chrono::milliseconds(3000);
  ProcessGroupNCCLTimedOutErrors pg(
      store_, 0, 1, options);

  auto work = pg.allreduce(tensors_);
  work->wait();
  EXPECT_TRUE(work->isSuccess());
  EXPECT_EQ(1, pg.getNCCLCommCacheSize());

  // Now run all reduce with errors.
  pg.set_timedout_error();
  work = pg.allreduce(tensors_);
  EXPECT_THROW(work->wait(), std::runtime_error);

  // Communicators might be aborted here, further operations would fail.
}

TEST_F(ProcessGroupNCCLErrorsTest, testNCCLErrorsNonBlocking) {
  if (skipTest()) {
    return;
  }

  c10d::ProcessGroupNCCL::Options options;
  options.opTimeout = std::chrono::milliseconds(3000);
  ProcessGroupNCCLSimulateErrors pg(
      store_, 0, 1, options);

  auto work = pg.allreduce(tensors_);
  pg.barrier()->wait();
  EXPECT_TRUE(work->isSuccess());
  EXPECT_EQ(1, pg.getNCCLCommCacheSize());

  // Now run all reduce with errors.
  pg.simulate_error();
  work = pg.allreduce(tensors_);

  // Should not throw exceptions.
  work->wait();
  pg.barrier()->wait();

  // Verify the work item failed.
  EXPECT_TRUE(work->isCompleted());
  EXPECT_FALSE(work->isSuccess());

  // Communicators might be aborted here, further operations would fail.
}
