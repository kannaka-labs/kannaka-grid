import importlib


def test_grid_package_imports():
    assert importlib.import_module("grid").__name__ == "grid"
