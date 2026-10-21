#include <zephyr/device.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>
#include <zephyr/usb/usbd.h>

#include "bench.h"

LOG_MODULE_REGISTER(bench_usb, LOG_LEVEL_INF);

BUILD_ASSERT(CONFIG_BENCH_USB_VID != 0 && CONFIG_BENCH_USB_PID != 0,
	     "CONFIG_BENCH_USB_VID and CONFIG_BENCH_USB_PID must be nonzero");
BUILD_ASSERT(!IS_ENABLED(CONFIG_FIDO2) || DT_NUM_INST_STATUS_OKAY(zephyr_hid_device) == 1,
	     "The FIDO2 firmware requires exactly one HID device");

USBD_DEVICE_DEFINE(bench_usb, DEVICE_DT_GET(DT_NODELABEL(zephyr_udc0)), CONFIG_BENCH_USB_VID,
		   CONFIG_BENCH_USB_PID);
USBD_DESC_LANG_DEFINE(bench_lang);
USBD_DESC_MANUFACTURER_DEFINE(bench_mfr, "FIDO2 research bench");
USBD_DESC_PRODUCT_DEFINE(bench_product, "Node maintenance authenticator");
USBD_DESC_CONFIG_DEFINE(bench_fs_desc, "Experiment interfaces");
USBD_DESC_CONFIG_DEFINE(bench_hs_desc, "Experiment interfaces");
USBD_CONFIGURATION_DEFINE(bench_fs, 0, 100, &bench_fs_desc);
USBD_CONFIGURATION_DEFINE(bench_hs, 0, 100, &bench_hs_desc);

static void usb_message(struct usbd_context *const ctx, const struct usbd_msg *const msg)
{
	int ret;

	if (!usbd_can_detect_vbus(ctx)) {
		return;
	}
	if (msg->type == USBD_MSG_VBUS_READY) {
		ret = usbd_enable(ctx);
		if (ret) {
			LOG_ERR("USB enable failed: %d", ret);
		}
	}
	if (msg->type == USBD_MSG_VBUS_REMOVED) {
		ret = usbd_disable(ctx);
		if (ret) {
			LOG_ERR("USB disable failed: %d", ret);
		}
	}
}

static int register_configuration(enum usbd_speed speed, struct usbd_config_node *config,
				  bool fido_ready)
{
	/* An unregistered HID device would prevent CDC from starting too. */
	const char *const blocked[] = {"dfu_dfu", fido_ready ? NULL : "hid_0", NULL};
	int ret;

	ret = usbd_add_configuration(&bench_usb, speed, config);
	if (ret) {
		LOG_ERR("USB configuration registration failed: %d", ret);
		return ret;
	}

	ret = usbd_register_all_classes(&bench_usb, speed, 1, blocked);
	if (ret) {
		LOG_ERR("USB class registration failed: %d", ret);
		return ret;
	}

	/* CDC ACM requires the IAD device class. */
	if (IS_ENABLED(CONFIG_USBD_CDC_ACM_CLASS)) {
		usbd_device_set_code_triple(&bench_usb, speed, USB_BCC_MISCELLANEOUS, 0x02, 0x01);
	}

	return 0;
}

int bench_usb_start(bool fido_ready)
{
	int ret;

	ret = usbd_add_descriptor(&bench_usb, &bench_lang);
	if (ret) {
		LOG_ERR("USB language descriptor failed: %d", ret);
		return ret;
	}

	ret = usbd_add_descriptor(&bench_usb, &bench_mfr);
	if (ret) {
		LOG_ERR("USB manufacturer descriptor failed: %d", ret);
		return ret;
	}

	ret = usbd_add_descriptor(&bench_usb, &bench_product);
	if (ret) {
		LOG_ERR("USB product descriptor failed: %d", ret);
		return ret;
	}

	ret = register_configuration(USBD_SPEED_FS, &bench_fs, fido_ready);
	if (ret) {
		return ret;
	}

	if (USBD_SUPPORTS_HIGH_SPEED && usbd_caps_speed(&bench_usb) == USBD_SPEED_HS) {
		ret = register_configuration(USBD_SPEED_HS, &bench_hs, fido_ready);
		if (ret) {
			return ret;
		}
	}

	ret = usbd_msg_register_cb(&bench_usb, usb_message);
	if (ret) {
		LOG_ERR("USB message callback registration failed: %d", ret);
		return ret;
	}

	ret = usbd_init(&bench_usb);
	if (ret) {
		LOG_ERR("USB initialization failed: %d", ret);
		return ret;
	}

	if (!usbd_can_detect_vbus(&bench_usb)) {
		ret = usbd_enable(&bench_usb);
		if (ret) {
			LOG_ERR("USB enable failed: %d", ret);
		}
		return ret;
	}

	return 0;
}
