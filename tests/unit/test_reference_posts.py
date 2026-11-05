import pytest
from custombuild_domain.rear_coverage import ReferenceConstruction
from custombuild_domain.reference_posts import ReferencePost, contact
from pydantic import ValidationError


def square(key="ref-post-1", **changes):
    return ReferencePost.model_validate(
        {
            "key": key,
            "name": "Vänster ändstöd",
            "shape": "rectangular",
            "x_um": 0,
            "y_um": 0,
            "z_um": 0,
            "width_um": 20_001,
            "depth_um": 20_001,
            "height_um": 300_000,
            **changes,
        }
    )


def test_circular_post_distinguishes_free_space_tangency_and_positive_overlap():
    a = square(shape="round")
    assert contact(a, square("ref-post-2", x_um=20_001, shape="round")) == "touch"
    assert contact(a, square("ref-post-2", x_um=20_000, shape="round")) == "overlap"
    assert contact(a, square("ref-post-2", x_um=19_000, y_um=19_000)) is None
    assert contact(a, square("ref-post-2", z_um=300_000)) == "touch"
    assert contact(a, square("ref-post-2", z_um=300_001)) is None
    with pytest.raises(ValidationError, match="diameter"):
        square(shape="round", depth_um=20_000)
    with pytest.raises(ValidationError, match="eget ID"):
        ReferenceConstruction(uprights="separate_posts", back="unknown", posts=(a, a))
