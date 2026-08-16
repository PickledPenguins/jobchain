"""Validation for the class-based example, written as Python instead of YAML.

Equivalent in every respect to declaring `fields:` in the schema YAML; this
form is worth choosing when a check is awkward to express declaratively
(here, that "fast" mode caps scale_factor below 2.0) or when the same
validation logic should be shared and unit tested outside jobchain.
"""

from jobchain import Field, Float, OneOf, PathExists, Regex, SchemaBase


class RunInput(SchemaBase):
    """Validation for the class-based single-job example."""

    fields = [
        Field("run_id", [Regex("[A-Za-z0-9_-]+")], unique=True),
        Field("dataset", [PathExists(must_be_file=True, readable=True)]),
        Field("mode", [OneOf(["fast", "accurate"])]),
        Field("scale_factor", [Float(min=0.0, max=10.0)]),
    ]

    def check_row(self, row):
        """Cross-column rule awkward to express as a single declarative check."""
        if row["mode"] == "fast" and row["scale_factor"] >= 2.0:
            return "fast mode requires scale_factor below 2.0"
        return None
