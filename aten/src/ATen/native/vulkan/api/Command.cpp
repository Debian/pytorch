#include <ATen/native/vulkan/api/Command.h>

namespace at {
namespace native {
namespace vulkan {
namespace api {

Command::Pool::Factory::Factory(const GPU& gpu)
  : device_(gpu.device) {
    TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
        device_,
        "Invalid Vulkan device!");
}

typename Command::Pool::Factory::Handle Command::Pool::Factory::operator()(
    const Descriptor& descriptor) const {
  const VkCommandPoolCreateInfo command_pool_create_info{
    VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
    nullptr,
    VK_COMMAND_POOL_CREATE_TRANSIENT_BIT,
    descriptor.queue_family_index,
  };

  VkCommandPool command_pool{};
  VK_CHECK(vkCreateCommandPool(
      device_,
      &command_pool_create_info,
      nullptr,
      &command_pool));

  TORCH_CHECK(
      command_pool,
      "Invalid Vulkan command pool!");

  return Handle{
    command_pool,
    Deleter(device_),
  };
}

void Command::Pool::purge(
    const VkDevice device,
    const VkCommandPool command_pool) {
  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      device,
      "Invalid Vulkan device!");

  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      command_pool,
      "Invalid Vulkan command pool!");

  VK_CHECK(vkResetCommandPool(device, command_pool, 0u));
}

namespace {

VkCommandBuffer allocate_command_buffer(
    const VkDevice device,
    const VkCommandPool command_pool) {
  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      device,
      "Invalid Vulkan device!");

  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      command_pool,
      "Invalid Vulkan command pool!");

  const VkCommandBufferAllocateInfo command_buffer_allocate_info{
    VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
    nullptr,
    command_pool,
    VK_COMMAND_BUFFER_LEVEL_PRIMARY,
    1u,
  };

  VkCommandBuffer command_buffer{};
  VK_CHECK(vkAllocateCommandBuffers(
      device,
      &command_buffer_allocate_info,
      &command_buffer));

  TORCH_CHECK(
      command_buffer,
      "Invalid Vulkan command buffer!");

  return command_buffer;
}

} // namespace

Command::Buffer::Buffer(const VkDevice device, const VkCommandPool command_pool)
  : command_buffer_(allocate_command_buffer(device, command_pool)) {
  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      command_buffer_,
      "Invalid Vulkan command buffer!");
}

void Command::Buffer::Buffer::begin() {
  const VkCommandBufferBeginInfo command_buffer_begin_info{
    VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
    nullptr,
    VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,
    nullptr,
  };

  VK_CHECK(vkBeginCommandBuffer(
      command_buffer_,
      &command_buffer_begin_info));
}

void Command::Buffer::Buffer::end() {
  VK_CHECK(vkEndCommandBuffer(command_buffer_));
}

void Command::Buffer::bind(const VkPipeline pipeline) {
  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      pipeline,
      "Invalid Vulkan pipeline!");

  vkCmdBindPipeline(
      command_buffer_,
      VK_PIPELINE_BIND_POINT_COMPUTE,
      pipeline);
}

void Command::Buffer::bind(
    const VkPipelineLayout pipeline_layout,
    const VkDescriptorSet descriptor_set) {
  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      pipeline_layout,
      "Invalid Vulkan pipeline layout!");

  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      descriptor_set,
      "Invalid Vulkan descriptor set!");

  vkCmdBindDescriptorSets(
      command_buffer_,
      VK_PIPELINE_BIND_POINT_COMPUTE,
      pipeline_layout,
      0u,
      1u,
      &descriptor_set,
      0u,
      nullptr);
}

} // namespace api
} // namespace vulkan
} // namespace native
} // namespace at
