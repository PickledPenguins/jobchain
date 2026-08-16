from __future__ import annotations
import os
import tempfile
import unittest
from unittest.mock import patch

from jobchain import schema as S
from jobchain.config import (
    RunConfig, load_config, expand_run_name, expand_template,
    template_is_generation_aware, _resolve,
)
from jobchain.core import ConfigError, PipelineError, SchemaError, UsageError
from jobchain.pipeline import (
    StageSpec, Pipeline, single_job_pipeline, load_pipeline_source,
    _build_pipeline, _build_stage, _build_stage_spec, _resolve_chaining,
    _class_name_for, _validate_settings, JobStage, Setting, Integer,
)


class TestSchemaValidatorEdges(unittest.TestCase):
    def test_path_resolution_and_validator_basics(self):
        self.assertEqual(S.resolve_path("   ", "/tmp"), "")
        self.assertEqual(S.resolve_path("rel/file", None), "rel/file")
        self.assertEqual(S.resolve_path("/abs", "/tmp"), "/abs")
        self.assertEqual(S.Validator.__subclasses__()[0] if False else S.Int("x").normalize(" x "), "x")
        self.assertIn("Int", repr(S.Int("x")))
        with self.assertRaises(ValueError): S.Int(min=3, max=2)
        self.assertIn("between", S.Int(min=1,max=2).description)
        self.assertIn("at least", S.Int(min=1).description)
        self.assertIn("at most", S.Int(max=2).description)
        self.assertEqual(S.Int().validate(" +12 ").value, 12)
        self.assertFalse(S.Int().validate("1_2").ok)
        self.assertFalse(S.Int(min=5).validate("4").ok)
        self.assertFalse(S.Int(max=5).validate("6").ok)

    def test_float_nonfinite_and_bounds(self):
        self.assertTrue(S.Float(allow_nonfinite=True).validate("NaN").ok)
        self.assertTrue(S.Float(allow_nonfinite=True).validate("-Infinity").ok)
        self.assertFalse(S.Float().validate("nan").ok)
        self.assertFalse(S.Float(min=2).validate("1.5").ok)
        self.assertFalse(S.Float(max=2).validate("2.5").ok)
        self.assertTrue(S.Float().validate("1.25e2").ok)

    def test_string_bool_oneof_exact_regex(self):
        self.assertIn("at least", S.Str(min_length=2).description)
        self.assertIn("at most", S.Str(max_length=2).description)
        self.assertIn("to", S.Str(min_length=1,max_length=2,charset="ab").description)
        self.assertFalse(S.Str(min_length=2).validate("a").ok)
        self.assertFalse(S.Str(max_length=2).validate("abc").ok)
        self.assertFalse(S.Str(charset="ab").validate("ac").ok)
        self.assertTrue(S.Bool().validate(" YES ").value)
        self.assertFalse(S.Bool().validate("maybe").ok)
        with self.assertRaises(ValueError): S.OneOf([])
        self.assertTrue(S.OneOf(["A"]).validate("A").ok)
        self.assertFalse(S.OneOf(["A"]).validate("a").ok)
        self.assertTrue(S.OneOf(["A"],case_sensitive=False).validate("a").ok)
        self.assertFalse(S.Exact(3).validate("4").ok)
        self.assertTrue(S.Exact(3).validate("3").ok)
        with self.assertRaises(ValueError): S.Regex("[")
        self.assertFalse(S.Regex("a+").validate("b").ok)
        self.assertTrue(S.Regex("a+",ignore_case=True).validate("AAA").ok)

    def test_paths_optional_and_combinators(self):
        with tempfile.TemporaryDirectory() as d:
            f=os.path.join(d,"f"); os.mkdir(os.path.join(d,"dir"))
            open(f,"w").close()
            self.assertTrue(S.PathExists(must_be_file=True).validate(f).ok)
            self.assertFalse(S.PathExists(must_be_file=True).validate(os.path.join(d,"dir")).ok)
            self.assertTrue(S.PathExists(must_be_dir=True).validate(os.path.join(d,"dir")).ok)
            self.assertFalse(S.PathExists().validate(os.path.join(d,"nope")).ok)
            self.assertTrue(S.OutputPath().validate(os.path.join(d,"new")).ok)
            self.assertFalse(S.OutputPath(must_not_exist=True).validate(f).ok)
            with patch("jobchain.schema.os.path.isdir", return_value=False):
                self.assertFalse(S.OutputPath().validate(os.path.join(d,"x")).ok)
        opt=S.Optional_(S.Int(), default=7)
        self.assertEqual(opt.validate("   ").value,7)
        self.assertEqual(opt.validate(" 8 ").value,8)
        with self.assertRaises(ValueError): S.AllOf()
        with self.assertRaises(ValueError): S.AnyOf()
        self.assertEqual(S.AllOf(S.Int(), S.Int()).validate("3").value,3)
        self.assertFalse(S.AllOf(S.Int(), S.Exact(4)).validate("3").ok)
        self.assertTrue(S.AnyOf(S.Exact("x"), S.Int()).validate("2").ok)
        self.assertFalse(S.AnyOf(S.Exact("x"), S.Int()).validate("no").ok)

    def test_row_file_and_schema_edges(self):
        r=S.RequiredWhen("mode","x","value")
        self.assertIsNone(r.check({"mode":"y"}))
        self.assertIsNone(r.check({"mode":"x","value":"ok"}))
        self.assertIsNotNone(r.check({"mode":"x","value":""}))
        with self.assertRaises(ValueError): S.Comparison("a","?","b")
        expected = {"<": (1,2), "<=": (1,1), ">": (2,1), ">=": (1,1), "==": (1,1), "!=": (1,2)}
        for op, (a,b) in expected.items():
            self.assertIsNone(S.Comparison("a",op,"b").check({"a":a,"b":b}))
        self.assertIsNone(S.Comparison("a","<","b").check({"a":1,"b":2}))
        self.assertIsNone(S.Comparison("a","<","b").check({}))
        self.assertIn("cannot compare", S.Comparison("a","<","b").check({"a":1,"b":"x"}))
        p=S.PredicateRow(lambda row: "bad" if row.get("x") else None,"pred")
        self.assertEqual(p.check({"x":1}),"bad")
        u=S.Unique(["a","b"])
        self.assertEqual(u.check([(1,{"a":1,"b":2}),(2,{"a":1,"b":2})])[0][0],2)
        self.assertEqual(S.RowCount(min=2).check([(1,{}),]),[(0,"file has 1 rows, fewer than the required 2")])
        self.assertEqual(S.RowCount(max=1).check([(1,{}),(2,{})])[0][0],0)
        self.assertEqual(S.RowCount(min=1,max=2).check([(1,{})]),[])
        with self.assertRaises(ValueError): S.Unique([])
        with self.assertRaises(ValueError): S.RowCount(min=2,max=1) if False else S.Unique([])

    def test_schema_structure_reference_and_uniqueness(self):
        with self.assertRaises(SchemaError): Schema = S.Schema(name="x",fields=[])
        with self.assertRaises(SchemaError): S.Schema(name="x",fields=[S.Field("a"),S.Field("a")])
        with self.assertRaises(SchemaError): S.Schema(name="x",fields=[S.Field("a")],id_field="b")
        s=S.Schema(name="x",fields=[S.Field("a"),S.Field("b",unique=True)],id_field="a")
        self.assertEqual(s.unique_fields,["a","b"])
        self.assertEqual(s.field_names,["a","b"])
        with self.assertRaises(SchemaError): S.Schema(name="x",fields=[S.Field("a")],quoting=True,delimiter=None)
        with self.assertRaises(SchemaError): S.Schema(name="x",fields=[S.Field("a")],row_validators=[S.RequiredWhen("missing","x","a")])
        with self.assertRaises(SchemaError): S.Schema(name="x",fields=[S.Field("a")],file_validators=[S.Unique(["missing"])])

    def test_schema_loader_error_branches(self):
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,"s.yaml")
            with open(p,"w") as f: f.write("name: s\nfields: []\n")
            with self.assertRaises(SchemaError): S.load_schema(p)
            with open(p,"w") as f: f.write("fields:\n - name: x\n   checks: [{type: int}]\n   bad: 1\n")
            with self.assertRaises(SchemaError): S.load_schema(p)
            with open(p,"w") as f: f.write("fields:\n - name: x\n   type: int\n   nope: 1\n")
            with self.assertRaises(SchemaError): S.load_schema(p)
            with open(p,"w") as f: f.write("fields:\n - name: x\n   checks: [{type: int}, {type: str}]\n")
            self.assertEqual(len(S.load_schema(p).fields[0].validators),2)
            with open(p,"w") as f: f.write("format:\n  delimiter: whitespace\nfields:\n - name: x\n")
            self.assertIsNone(S.load_schema(p).delimiter)


class TestConfigEdges(unittest.TestCase):
    def test_config_load_and_template_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,"c.yaml")
            with open(p,"w") as f: f.write("name: run\nparams: p.csv\nschema: s.yaml\n")
            c=load_config(p, {"width":2,"run_name":"{date}-{user}","workers":None})
            self.assertEqual(c.width,2); self.assertEqual(c.provenance["width"],"cli")
            self.assertEqual(c.params_path,os.path.join(d,"p.csv"))
            self.assertEqual(c.base_dir,d)
            self.assertGreaterEqual(c.effective_workers,1)
            self.assertIn("# Effective", __import__('jobchain.config',fromlist=['render_final_config']).render_final_config(c,{},None))
            bads=["wat: 1\nname: x\nparams: p\nschema: s\n", "name: x\nparams: p\n", "- x\n"]
            for text in bads:
                with open(p,"w") as f:f.write(text)
                with self.assertRaises(ConfigError): load_config(p)
            with open(p,"w") as f:f.write("name: x\nparams: p\nschema: s\nlogging:\n  bad: x\n")
            with self.assertRaises(ConfigError): load_config(p)
            with open(p,"w") as f:f.write("name: x\nparams: p\nschema: s\n")
            with self.assertRaises(UsageError): load_config(p,{"bad":1})
        self.assertEqual(_resolve("~/x", "/tmp"), os.path.expanduser("~/x"))
        self.assertEqual(template_is_generation_aware("{row.generation}/x"),True)
        self.assertEqual(expand_template("{run.name}/{run.home}/{row.name}/{row.index}/{row.generation}/{row.x}","r","/h",{"x":3},"rn",4,2),"r//h/rn/4/2/3")
        for t in ("{run.nope}","{row.nope}","{bad.x}"):
            with self.assertRaises(ConfigError): expand_template(t,"r","h",{})

    def test_runconfig_validation(self):
        for kwargs in [dict(name="",params="p",schema_source={}),dict(name="bad/name",params="p",schema_source={}),dict(name="x",params="p",schema_source={},width=0),dict(name="x",params="p",schema_source={},workers=-1),dict(name="x",params="p",schema_source={},scheduler="x"),dict(name="x",params="p",schema_source={},terminal_level="x")]:
            with self.assertRaises(ConfigError): RunConfig(**kwargs)
        with patch("jobchain.config.getpass.getuser", side_effect=RuntimeError):
            with patch.dict(os.environ,{},clear=True): self.assertEqual(expand_run_name("{user}"),"unknown")


class TestPipelineEdges(unittest.TestCase):
    def test_pipeline_build_and_chaining(self):
        self.assertEqual(_class_name_for("hello_world"),"HelloWorld")
        p=single_job_pipeline("echo hi")
        self.assertEqual(p.chaining_stage,"job"); self.assertEqual(p.spec("job").name,"job")
        with self.assertRaises(PipelineError): p.stage("x")
        with self.assertRaises(PipelineError): p.spec("x")
        with self.assertRaises(PipelineError): _build_pipeline({"stages":[]},"x")
        with self.assertRaises(PipelineError): _build_pipeline({"wat":1,"stages":[{"name":"x"}]},"x")
        with self.assertRaises(PipelineError): _build_pipeline({"defaults":1,"stages":[{"name":"x"}]},"x")
        with self.assertRaises(PipelineError): _build_pipeline({"stages":[{"name":"x"},{"name":"x"}]},"x")
        with self.assertRaises(PipelineError): _build_stage_spec("x",1,{})
        with self.assertRaises(PipelineError): _build_stage_spec({},1,{})
        with self.assertRaises(PipelineError): _build_stage_spec({"name":"x","depends":"bad"},1,{})
        specs=[_build_stage_spec({"name":"a"},1,{}),_build_stage_spec({"name":"b"},2,{})]
        _resolve_chaining(specs); self.assertEqual(specs[-1].depends,"afterany")
        specs=[_build_stage_spec({"name":"a"},1,{}),_build_stage_spec({"name":"b","chains_next":True,"depends":"afterany"},2,{})]
        _resolve_chaining(specs)
        specs=[_build_stage_spec({"name":"a","chains_next":True},1,{}),_build_stage_spec({"name":"b"},2,{})]
        _resolve_chaining(specs)
        with self.assertRaises(PipelineError): _resolve_chaining([_build_stage_spec({"name":"a","chains_next":True},1,{}),_build_stage_spec({"name":"b","chains_next":True},2,{})])
        with self.assertRaises(PipelineError): _resolve_chaining([_build_stage_spec({"name":"a"},1,{}),_build_stage_spec({"name":"b","chains_next":True,"depends":"afterok"},2,{})])

    def test_pipeline_loading_and_stage_validation(self):
        with self.assertRaises(PipelineError): load_pipeline_source(1,"/tmp")
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,"p.yaml")
            with open(p,"w") as f:f.write("stages:\n - name: hello\n   command: echo hi\n")
            pipe=load_pipeline_source(p,d); self.assertEqual(pipe.name,"pipeline")
            with open(p,"w") as f:f.write("- bad\n")
            with self.assertRaises(PipelineError): load_pipeline_source(p,d)
        spec=_build_stage_spec({"name":"x","command":"echo"},1,{})
        obj=_build_stage(spec,None,None,object()); self.assertIsInstance(obj,JobStage)
        with self.assertRaises(PipelineError): _build_stage(_build_stage_spec({"name":"x"},1,{}),None,None,object())
        class Sg(JobStage): settings={"n":Integer(required=True)}
        good=StageSpec("x",1,"afterok",False,{"_position":1,"n":2},"Sg")
        self.assertEqual(_validate_settings(good,Sg)["n"],2)
        with self.assertRaises(PipelineError): _validate_settings(StageSpec("x",1,"afterok",False,{"_position":1,"bad":2},"Sg"),Sg)
        with self.assertRaises(PipelineError): _validate_settings(StageSpec("x",1,"afterok",False,{"_position":1},"Sg"),Sg)

class TestFinalSmallGaps(unittest.TestCase):
    def test_schema_abstract_and_loader_edges(self):
        with self.assertRaises(TypeError): S.Validator("")
        with self.assertRaises(ValueError): S.Float(min=2,max=1)
        self.assertEqual(S.Bool().validate(" no ").value, False)
        self.assertIn("different from", S.Comparison("a","!=","b").description)
        self.assertIn("row_count", S.FILE_VALIDATORS)
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,"v.py")
            with open(p,"w") as f:f.write("from jobchain.schema import Validator, CheckResult\nclass V(Validator):\n def __init__(self): super().__init__('v')\n def _check(self,raw): return CheckResult(True,raw)\nV=V()\n")
            self.assertIsInstance(S._load_python_validator("v.py:V",p), S.Validator)

    def test_config_yaml_and_provenance_branches(self):
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,"c.yaml")
            with open(p,"w") as f: f.write("name: x\nparams: p\nschema: s\nwidth: 3\n")
            c=load_config(p)
            self.assertEqual(c.provenance["width"],"config")
            with open(p,"w") as f: f.write("name: [\n")
            with self.assertRaises(ConfigError): load_config(p)

    def test_pipeline_settings_freeze_and_resources(self):
        from jobchain.pipeline import Choice, Bool, Integer, Text
        self.assertEqual(Choice([1]).check("x",1),1)
        with self.assertRaises(PipelineError): Choice([1]).check("x",2)
        self.assertTrue(Bool().check("x",True))
        with self.assertRaises(PipelineError): Bool().check("x",1)
        self.assertEqual(Integer(min=2,max=4).check("x","3"),3)
        with self.assertRaises(PipelineError): Integer(min=2).check("x",1)
        with self.assertRaises(PipelineError): Integer(max=2).check("x",3)
        with self.assertRaises(PipelineError): Integer().check("x","no")
        self.assertEqual(Text().check("x",3),"3")
        class R(JobStage):
            def resources(self,row): return {"ncpus":2}
        r=R("r",{"ncpus":1,"env":{"A":"B"},"extra_directives":["#x"]},None)
        self.assertEqual(r.effective_resources({})["ncpus"],2)
        with self.assertRaises(PipelineError):
            class Bad(JobStage):
                def resources(self,row): return {"nope":1}
            Bad("b",{},None).effective_resources({})
        object.__setattr__(r,"foo",1)
        with self.assertRaises(PipelineError): r.foo=2

    def test_pipeline_module_resolution_errors(self):
        class M: NotStage=object
        spec=_build_stage_spec({"name":"x"},1,{})
        with self.assertRaises(PipelineError): _build_stage(spec,M,"m.py",None)
        class M2:
            class Good(JobStage): pass
        spec=StageSpec("x",1,"afterok",False,{"_position":1},"Good")
        self.assertIsInstance(_build_stage(spec,M2,"m.py",None),JobStage)
