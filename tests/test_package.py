"""Package-level smoke checks."""

import pyvisor


def test_package_imports() -> None:
    assert pyvisor.__doc__ == "Pyvisor service package."
