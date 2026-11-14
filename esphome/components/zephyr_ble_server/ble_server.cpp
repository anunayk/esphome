#ifdef USE_ZEPHYR
#include "ble_server.h"
#include "esphome/core/application.h"
#include "esphome/core/defines.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/settings/settings.h>

#ifdef USE_OTA_STATE_LISTENER
#include "esphome/components/ota/ota_backend.h"
#endif

namespace esphome::zephyr_ble_server {

ESPHOME_LOG_TAG(TAG, "zephyr_ble_server");

static k_work advertise_work;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

BLEServer *global_ble_server;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

// The controller supplies flags; reserve two bytes for the name header.
static constexpr size_t MAX_ADV_NAME_LEN = BT_GAP_ADV_MAX_ADV_DATA_LEN - 2;

static const bt_data SD[] = {
#ifdef USE_OTA
    BT_DATA_BYTES(BT_DATA_UUID128_ALL, 0x84, 0xaa, 0x60, 0x74, 0x52, 0x8a, 0x8b, 0x86, 0xd3, 0x4c, 0xb7, 0x1d, 0x1d,
                  0xdc, 0x53, 0x8d),
#endif
};

#ifdef BT_LE_ADV_CONN_FAST_2
const bt_le_adv_param *const ADV_PARAM = BT_LE_ADV_CONN_FAST_2;
#else
const bt_le_adv_param *const ADV_PARAM = BT_LE_ADV_CONN;
#endif

// Last bt_le_adv_start() rc, surfaced in dump_config() because advertise() runs on
// the BT workqueue thread whose logs don't reliably flush over USB-CDC. -255 means
// advertise() has not run yet; 0 means advertising is up. Useful on a headless BLE
// node where a non-zero rc is otherwise invisible.
static volatile int advertise_rc = -255;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

static void advertise(k_work *work) {
  int rc = bt_le_adv_stop();
  if (rc) {
    ESP_LOGE(TAG, "Advertising failed to stop (rc %d)", rc);
  }

  if (global_ble_server != nullptr && !global_ble_server->is_advertising_wanted()) {
    advertise_rc = -1;
    return;
  }
  // Zephyr would cut the end of a long name, which is where the MAC suffix is
  char name[MAX_ADV_NAME_LEN + 1];
  const auto &app_name = App.get_name();
  const size_t name_len =
      truncate_name_to(name, sizeof(name), app_name.c_str(), app_name.length(),
                       App.is_name_add_mac_suffix_enabled() ? MAC_ADDRESS_SUFFIX_WITH_SEPARATOR_LEN : 0);
  const uint8_t name_type = name_len < app_name.length() ? BT_DATA_NAME_SHORTENED : BT_DATA_NAME_COMPLETE;
  const bt_data ad[] = {
      BT_DATA(name_type, name, static_cast<uint8_t>(name_len)),
  };

  rc = bt_le_adv_start(ADV_PARAM, ad, ARRAY_SIZE(ad), SD, ARRAY_SIZE(SD));
  advertise_rc = rc;
  if (rc) {
    ESP_LOGE(TAG, "Advertising failed to start (rc %d)", rc);
    return;
  }
  ESP_LOGI(TAG, "Advertising successfully started as '%s'", name);
}

void BLEServer::connected(bt_conn *conn, uint8_t err) {
  char addr[BT_ADDR_LE_STR_LEN];
  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));
  if (err) {
    ESP_LOGE(TAG, "Failed to connect to %s (%u)", addr, err);
    return;
  }
  ESP_LOGI(TAG, "Connected %s", addr);
#ifdef CONFIG_BT_SMP
  if (bt_conn_set_security(conn, BT_SECURITY_L4)) {
    ESP_LOGE(TAG, "Failed to set security");
  }
#endif
  conn = bt_conn_ref(conn);
  global_ble_server->defer([conn]() { global_ble_server->set_conn_(conn); });
}

void BLEServer::disconnected(bt_conn *conn, uint8_t reason) {
  char addr[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

  ESP_LOGI(TAG, "Disconnected from %s (reason 0x%02x)", addr, reason);
  conn = bt_conn_ref(conn);
  global_ble_server->defer([conn]() {
    if (global_ble_server->conn_ == conn) {
      bt_conn_unref(conn);
      global_ble_server->conn_ = nullptr;
    }
    bt_conn_unref(conn);
  });
}

void BLEServer::set_conn_(bt_conn *conn) {
  // The disconnect may have run first; deferred callbacks are not ordered on this platform.
  bt_conn_info info;
  if (bt_conn_get_info(conn, &info) != 0 || info.state != BT_CONN_STATE_CONNECTED) {
    bt_conn_unref(conn);
    return;
  }
  if (this->conn_ != nullptr) {
    bt_conn_unref(this->conn_);
  }
  this->conn_ = conn;
}
// Restart advertising when the stack frees the connection object, not on disconnect.
// With BT_LE_ADV_OPT_CONN (NCS 3.x), the stack does not resume advertising itself, and a
// start from the disconnected callback fails while the connection object is still in use.
static void recycled() { k_work_submit(&advertise_work); }

#ifdef CONFIG_BT_SMP
static void identity_resolved(bt_conn *conn, const bt_addr_le_t *rpa, const bt_addr_le_t *identity) {
  char addr_identity[BT_ADDR_LE_STR_LEN];
  char addr_rpa[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(identity, addr_identity, sizeof(addr_identity));
  bt_addr_le_to_str(rpa, addr_rpa, sizeof(addr_rpa));

  ESP_LOGD(TAG, "Identity resolved %s -> %s", addr_rpa, addr_identity);
}

static void security_changed(bt_conn *conn, bt_security_t level, bt_security_err err) {
  char addr[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

  if (!err) {
    ESP_LOGD(TAG, "Security changed: %s level %u", addr, level);
  } else {
    ESP_LOGE(TAG, "Security failed: %s level %u err %d", addr, level, err);
  }
}

static void pairing_complete(bt_conn *conn, bool bonded) {
  char addr[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

  ESP_LOGD(TAG, "Pairing completed: %s, bonded: %d", addr, bonded);
}

static void pairing_failed(bt_conn *conn, bt_security_err reason) {
  char addr[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

  ESP_LOGE(TAG, "Pairing failed conn: %s, reason %d", addr, reason);

  bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
}

static void bond_deleted(uint8_t id, const bt_addr_le_t *peer) {
  char addr[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(peer, addr, sizeof(addr));
  ESP_LOGD(TAG, "Bond deleted for %s, id %u", addr, id);
}

static void auth_passkey_display(bt_conn *conn, unsigned int passkey) {
  char addr[BT_ADDR_LE_STR_LEN];
  char passkey_str[7];

  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

  snprintk(passkey_str, 7, "%06u", passkey);

  ESP_LOGI(TAG, "Passkey for %s: %s", addr, passkey_str);
}

static void conn_addr_str(bt_conn *conn, char *addr, size_t len) {
  struct bt_conn_info info;

  if (bt_conn_get_info(conn, &info) < 0) {
    addr[0] = '\0';
    return;
  }

  switch (info.type) {
    case BT_CONN_TYPE_LE:
      bt_addr_le_to_str(info.le.dst, addr, len);
      break;
    default:
      ESP_LOGE(TAG, "Not implemented");
      addr[0] = '\0';
      break;
  }
}

static void auth_cancel(bt_conn *conn) {
  char addr[BT_ADDR_LE_STR_LEN];

  conn_addr_str(conn, addr, sizeof(addr));

  ESP_LOGI(TAG, "Pairing cancelled: %s", addr);
}

void BLEServer::auth_passkey_confirm(bt_conn *conn, unsigned int passkey) {
  char addr[BT_ADDR_LE_STR_LEN];
  char passkey_str[7];

  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

  snprintk(passkey_str, 7, "%06u", passkey);

  ESP_LOGI(TAG, "Confirm passkey for %s: %s", addr, passkey_str);
  global_ble_server->defer([passkey]() {
    if (global_ble_server->passkey_cb_.size() == 0) {
      ESP_LOGW(TAG, "Nothing answers numeric comparison (safe mode skips the automation), rejecting pairing");
      global_ble_server->numeric_comparison_reply(false);
      return;
    }
    global_ble_server->passkey_cb_(passkey);
  });
}

static void auth_pairing_confirm(bt_conn *conn) {
  /* Automatically confirm pairing request from the device side. */
  auto err = bt_conn_auth_pairing_confirm(conn);
  if (err) {
    ESP_LOGE(TAG, "Can't confirm pairing (err: %d)", err);
    return;
  }

  char addr[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

  ESP_LOGI(TAG, "Pairing confirmed: %s", addr);
}

#endif

void BLEServer::setup() {
  global_ble_server = this;
  int err = 0;
  k_work_init(&advertise_work, advertise);

  static bt_conn_cb conn_callbacks = {
      .connected = connected,
      .disconnected = disconnected,
      .recycled = recycled,
#ifdef CONFIG_BT_SMP
      .identity_resolved = identity_resolved,
      .security_changed = security_changed,
#endif
  };

  bt_conn_cb_register(&conn_callbacks);
#ifdef CONFIG_BT_SMP
  static struct bt_conn_auth_info_cb conn_auth_info_callbacks = {
      .pairing_complete = pairing_complete, .pairing_failed = pairing_failed, .bond_deleted = bond_deleted};
  err = bt_conn_auth_info_cb_register(&conn_auth_info_callbacks);
  if (err) {
    ESP_LOGE(TAG, "Failed to register authorization info callbacks.");
  }
  static struct bt_conn_auth_cb auth_cb = {
      .passkey_display = auth_passkey_display,
      .passkey_confirm = auth_passkey_confirm,
      .cancel = auth_cancel,
      .pairing_confirm = auth_pairing_confirm,
  };
  err = bt_conn_auth_cb_register(&auth_cb);
  if (err) {
    ESP_LOGE(TAG, "Failed to set auth handlers (%d)", err);
  }
#endif
  // callback cannot be used to start scanning due to race conditions with BT_SETTINGS
  err = bt_enable(nullptr);
  if (err) {
    ESP_LOGE(TAG, "Bluetooth enable failed: %d", err);
    return;
  }
#ifdef CONFIG_BT_SETTINGS
  err = settings_load();
  if (err) {
    ESP_LOGE(TAG, "Cannot load settings, err: %d", err);
  }
#endif
  // Capture the runtime name (with the name_add_mac_suffix tail) before the
  // first advertise. Also push it to the GAP Device Name characteristic so a
  // connected central reads the same per-device name (needs
  // CONFIG_BT_DEVICE_NAME_DYNAMIC).
  err = bt_set_name(App.get_name().c_str());
  if (err) {
    ESP_LOGW(TAG, "Failed to set GAP device name to '%s' (err %d)", App.get_name().c_str(), err);
  }
#ifdef USE_OTA_STATE_LISTENER
  // Observe OTA across all transports so disconnect-on-stop can stand down while a
  // firmware update is in flight (defense-in-depth; the ble_policy predicate also
  // keeps advertising up during OTA).
  ota::get_global_ota_callback()->add_global_state_listener(this);
#endif
  k_work_submit(&advertise_work);
}

#ifdef ESPHOME_LOG_HAS_DEBUG
static const char *role_str(uint8_t role) {
  switch (role) {
    case BT_CONN_ROLE_CENTRAL:
      return "Central";
    case BT_CONN_ROLE_PERIPHERAL:
      return "Peripheral";
    default:
      break;
  }

  return "Unknown";
}

static void connection_info(bt_conn *conn, void *user_data) {
  char addr[BT_ADDR_LE_STR_LEN];
  struct bt_conn_info info;

  if (bt_conn_get_info(conn, &info) < 0) {
    ESP_LOGE(TAG, "Unable to get info: conn %p", conn);
    return;
  }

  switch (info.type) {
    case BT_CONN_TYPE_LE:
      bt_addr_le_to_str(info.le.dst, addr, sizeof(addr));
      ESP_LOGD(TAG, "  %u [LE][%s] %s: Interval %u latency %u timeout %u security L%u", info.id, role_str(info.role),
               addr, info.le.interval, info.le.latency, info.le.timeout, info.security.level);
      break;
    default:
      ESP_LOGE(TAG, "Not implemented");
      break;
  }
}
#ifdef CONFIG_BT_BONDABLE
static void bond_info(const struct bt_bond_info *info, void *user_data) {
  char addr[BT_ADDR_LE_STR_LEN];

  bt_addr_le_to_str(&info->addr, addr, sizeof(addr));
  ESP_LOGD(TAG, "  Bond remote identity: %s", addr);
}
#endif
#endif

void BLEServer::dump_config() {
  ESP_LOGCONFIG(TAG,
                "ble server:\n"
                "  connected: %s\n"
                "  name: %s\n"
                "  appearance: %u\n"
                "  ready: %s\n"
                "  last advertise rc: %d (-255 = advertise() never ran)\n"
                "  id0 addr: %s\n"
#ifdef CONFIG_BT_SMP
                "  security manager: YES",
#else
                "  security manager: NO",
#endif
                YESNO(this->conn_), bt_get_name(), bt_get_appearance(), YESNO(bt_is_ready()), (int) advertise_rc,
                ([]() -> const char * {
                  static char s[BT_ADDR_LE_STR_LEN] = "none";
                  bt_addr_le_t ids[CONFIG_BT_ID_MAX];
                  size_t n = CONFIG_BT_ID_MAX;
                  bt_id_get(ids, &n);
                  if (n > 0)
                    bt_addr_le_to_str(&ids[0], s, sizeof(s));
                  return s;
                })());

#ifdef ESPHOME_LOG_HAS_DEBUG
  bt_conn_foreach(BT_CONN_TYPE_ALL, connection_info, nullptr);
#ifdef CONFIG_BT_BONDABLE
  bt_foreach_bond(BT_ID_DEFAULT, bond_info, nullptr);
#endif
#endif
}

void BLEServer::set_advertising_enabled(bool enabled) {
  // Runs on the main loop (driven from an ESPHome action), the same context that
  // mutates conn_ via defer(), so reading conn_ here is race-free.
  if (this->advertising_wanted_ == enabled) {
    ESP_LOGD(TAG, "Advertising already %s", enabled ? "enabled" : "disabled");
    return;
  }
  this->advertising_wanted_ = enabled;
  // Re-run advertise() on the BT workqueue either way: it starts when wanted, or
  // stops-and-stays-down when not (see the latch check at the top of advertise()).
  k_work_submit(&advertise_work);
  if (enabled) {
    ESP_LOGI(TAG, "Advertising enabled (link-gated on)");
    return;
  }
  ESP_LOGI(TAG, "Advertising disabled (link-gated off)");
  // Disconnect-on-stop: drop an active central so "off" closes the surface, mirroring
  // wifi.disable tearing down the AP. Skip while an OTA is mid-flight so a remote
  // firmware update is never interrupted (the connection, not the advert, carries it).
  if (this->conn_ == nullptr)
    return;
  if (this->ota_in_progress_) {
    ESP_LOGW(TAG, "Advertising off requested during OTA; keeping the BLE connection up");
    return;
  }
  ESP_LOGI(TAG, "Disconnecting active central (advertising off)");
  bt_conn_disconnect(this->conn_, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
}

#ifdef USE_OTA_STATE_LISTENER
void BLEServer::on_ota_global_state(ota::OTAState state, float progress, uint8_t error, ota::OTAComponent *comp) {
  if (state == ota::OTA_STARTED) {
    this->ota_in_progress_ = true;
  } else if (state == ota::OTA_COMPLETED || state == ota::OTA_ERROR || state == ota::OTA_ABORT) {
    this->ota_in_progress_ = false;
  }
}
#endif

void BLEServer::numeric_comparison_reply(bool accept) {
  if (this->conn_ == nullptr) {
    ESP_LOGE(TAG, "Not connected");
    return;
  }
  ESP_LOGD(TAG, "Numeric comparison %s", accept ? "accepted" : "rejected");
  if (accept) {
    bt_conn_auth_passkey_confirm(this->conn_);
  } else {
    bt_conn_auth_cancel(this->conn_);
  }
}

}  // namespace esphome::zephyr_ble_server

#endif
