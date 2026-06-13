#pragma once

#include "packet_transport.h"

#include "esphome/core/automation.h"

#include <string>
#include <utility>
#include <vector>

namespace esphome::packet_transport {

/// Register (and optionally key) a provider at runtime. The name is copied, and the optional
/// encryption key must be the 32-byte key used by the peer (i.e. already hashed/derived).
template<typename... Ts> class AddProviderAction : public Action<Ts...>, public Parented<PacketTransport> {
 public:
  TEMPLATABLE_VALUE(std::string, name)
  TEMPLATABLE_VALUE(std::vector<uint8_t>, encryption_key)

  void play(Ts... x) override {
    auto name = this->name_.value(x...);
    this->parent_->add_provider(name.c_str());
    if (this->encryption_key_.has_value()) {
      auto key = this->encryption_key_.value(x...);
      if (!key.empty())
        this->parent_->set_provider_encryption(name.c_str(), std::move(key));
    }
  }
};

/// Remove a provider (and any sensor subscriptions registered against it) at runtime.
template<typename... Ts> class RemoveProviderAction : public Action<Ts...>, public Parented<PacketTransport> {
 public:
  TEMPLATABLE_VALUE(std::string, name)

  void play(Ts... x) override { this->parent_->remove_provider(this->name_.value(x...)); }
};

#if defined(USE_SENSOR) || defined(USE_BINARY_SENSOR)
/// Switch the source provider of a remote sensor or binary sensor at runtime. The remote id
/// (the name the value is broadcast under) is preserved; only the source peer changes.
template<typename... Ts> class SetProviderAction : public Action<Ts...>, public Parented<PacketTransport> {
 public:
  TEMPLATABLE_VALUE(std::string, provider)

#ifdef USE_SENSOR
  void set_sensor(sensor::Sensor *sensor) { this->sensor_ = sensor; }
#endif
#ifdef USE_BINARY_SENSOR
  void set_binary_sensor(binary_sensor::BinarySensor *binary_sensor) { this->binary_sensor_ = binary_sensor; }
#endif

  void play(Ts... x) override {
    auto provider = this->provider_.value(x...);
#ifdef USE_SENSOR
    if (this->sensor_ != nullptr)
      this->parent_->set_sensor_provider(this->sensor_, provider);
#endif
#ifdef USE_BINARY_SENSOR
    if (this->binary_sensor_ != nullptr)
      this->parent_->set_binary_sensor_provider(this->binary_sensor_, provider);
#endif
  }

 protected:
#ifdef USE_SENSOR
  sensor::Sensor *sensor_{nullptr};
#endif
#ifdef USE_BINARY_SENSOR
  binary_sensor::BinarySensor *binary_sensor_{nullptr};
#endif
};
#endif

}  // namespace esphome::packet_transport
