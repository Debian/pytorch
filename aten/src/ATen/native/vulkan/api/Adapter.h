#pragma once

#include <ATen/native/vulkan/api/Common.h>
#include <ATen/native/vulkan/api/Runtime.h>

namespace at {
namespace native {
namespace vulkan {
namespace api {

//
// A Vulkan Adapter represents a physical device and its properties.  Adapters
// are enumerated through the Runtime and are used in creation of Contexts.
// Each tensor in PyTorch is associated with a Context to make the
// device <-> tensor affinity explicit.
//

struct Adapter final {
  Runtime* runtime;
  VkPhysicalDevice handle;
  VkPhysicalDeviceProperties properties;
  VkPhysicalDeviceMemoryProperties memory_properties;
  uint32_t compute_queue_family_index;

  inline bool has_unified_memory() const {
    // Ideally iterate over all memory types to see if there is a pool that
    // is both host-visible, and device-local.  This should be a good proxy
    // for now.
    return VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU == properties.deviceType;
  }
};

} // namespace api
} // namespace vulkan
} // namespace native
} // namespace at
