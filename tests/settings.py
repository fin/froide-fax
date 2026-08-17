from configurations import values

from froide.settings import Test as FroideTest


class Test(FroideTest):
    INSTALLED_APPS = values.ListValue(
        FroideTest.INSTALLED_APPS.default + ["froide_fax"]
    )

    TELNYX_API_KEY = "test-api-key"
    TELNYX_APP_ID = "test-app-id"
    TELNYX_FROM_NUMBER = "+4930000000"
    # 32 zero bytes, base64 -- a syntactically valid Ed25519 public key.
    TELNYX_PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    @property
    def FROIDE_CONFIG(self):
        config = super().FROIDE_CONFIG
        config["message_handlers"] = dict(config["message_handlers"])
        config["message_handlers"]["fax"] = "froide_fax.fax.FaxMessageHandler"
        return config
