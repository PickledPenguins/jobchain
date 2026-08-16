import unittest
from jobchain.pipeline import JobStage

class TestPipelineFinalBranch(unittest.TestCase):
    def test_unfrozen_stage_setattr_uses_object_setattr(self):
        stage=object.__new__(JobStage)
        object.__setattr__(stage,"_frozen",False)
        stage.name="x"
        self.assertEqual(stage.name,"x")
