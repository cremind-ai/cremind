"""A tag enrolled over SWD on this computer, as a gateway worker here imports it
(app.tags.runtime.enroll.local): the hardware tools' inventory row and secret,
read without changing the tools' data."""

from __future__ import annotations

from app.tags.runtime.config import load_config
from app.tags.runtime.daemon.schema import open_database
from app.tags.runtime.enroll.local import local_enrollment
from app.tags.runtime.secrets import SecretStore
from app.tags.runtime.store.db import TagRecord

TAG = 0x1A2B3C4D


def _config(tmp_path):
    return load_config(tmp_path / "config.toml", env={"CREMIND_TAG_DATA_DIR": str(tmp_path / "data"),
                                                       "CREMIND_TAG_SECRETS_BACKEND": "file"})


def test_the_tools_enrollment_is_found_and_nothing_else(tmp_path) -> None:
    config = _config(tmp_path)
    assert local_enrollment(TAG, config) is None  # the tools never ran here
    assert not config.data_dir.exists()  # and nothing was created looking
    secrets = SecretStore.open(config.ensure_data_dir(), "file")
    ref = secrets.set_tag_secret(TAG, bytes(range(32)))
    db = open_database(config.db_path)
    db.insert_tag(TagRecord(tag_id=TAG, board=19, panel=3, width=128, height=250, planes=2, plane_flags=3,
                            secret_ref=ref, name="Shelf"))
    db.close()

    found = local_enrollment(TAG, config)
    assert found is not None and found.secret == bytes(range(32))
    assert (found.record.board, found.record.panel, found.record.width, found.record.height) == (19, 3, 128, 250)
    assert found.record.name == "Shelf"
    assert local_enrollment(0x11111111, config) is None
    # A record whose secret is gone is no enrollment: the tag could not be talked to.
    secrets.delete_tag_secret(TAG)
    assert local_enrollment(TAG, config) is None
