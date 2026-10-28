"""CFB_DATA_DIR redirects the whole store, so provisional runs cannot touch real data."""
import importlib

from utils import store


def test_the_data_dir_override_redirects_raw_and_computed(tmp_path, monkeypatch):
    monkeypatch.setenv("CFB_DATA_DIR", str(tmp_path))
    try:
        importlib.reload(store)
        assert store.RAW_DIR == tmp_path / "raw"
        assert store.COMPUTED_DIR == tmp_path / "computed"
    finally:
        monkeypatch.delenv("CFB_DATA_DIR")
        importlib.reload(store)


def test_unset_means_the_pipeline_data_folder():
    importlib.reload(store)
    assert store.RAW_DIR.parts[-2:] == ("data", "raw")
    assert store.COMPUTED_DIR.parts[-2:] == ("data", "computed")
