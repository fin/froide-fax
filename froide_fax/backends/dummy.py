"""Discards faxes silently. The analogue of Django's dummy email backend."""

from .base import RecordingFaxBackend


class DummyFaxBackend(RecordingFaxBackend):
    pass
