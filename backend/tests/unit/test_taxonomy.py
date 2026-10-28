"""单元 2：分类字典（taxonomy）纯函数测试。

分类字典是「后端校验 / 前端下拉 / 检索过滤」的唯一权威来源，
值一旦改动会影响历史数据与对外契约（document_type），所以这里把值锁死。
"""
from __future__ import annotations

from app.core import taxonomy


class TestBusinessDomains:
    def test_six_domains(self):
        assert len(taxonomy.BUSINESS_DOMAINS) == 6

    def test_values_are_unique(self):
        values = [c.value for c in taxonomy.BUSINESS_DOMAINS]
        assert len(values) == len(set(values))

    def test_expected_values(self):
        assert taxonomy.DOMAIN_VALUES == {
            "general", "flood", "water", "construction", "operation", "ecology",
        }

    def test_general_is_default(self):
        assert taxonomy.DEFAULT_DOMAIN == "general"
        assert taxonomy.DEFAULT_DOMAIN in taxonomy.DOMAIN_VALUES

    def test_normalize_invalid_to_default(self):
        assert taxonomy.normalize_domain(None) == "general"
        assert taxonomy.normalize_domain("") == "general"
        assert taxonomy.normalize_domain("不存在的域") == "general"
        assert taxonomy.normalize_domain("flood") == "flood"

    def test_labels(self):
        assert taxonomy.domain_label("flood") == "防洪减灾"
        assert taxonomy.domain_label(None) == "综合与基础"
        assert taxonomy.domain_label("未知") == "综合与基础"


class TestDocTypes:
    def test_ten_types(self):
        assert len(taxonomy.DOC_TYPES) == 10

    def test_legacy_values_kept(self):
        """历史 4 个值必须保留：旧数据与对外契约 document_type 依赖它们。"""
        for legacy in ("textbook", "standard", "manual", "other"):
            assert legacy in taxonomy.DOC_TYPE_VALUES

    def test_expected_values(self):
        assert taxonomy.DOC_TYPE_VALUES == {
            "standard", "regulation", "textbook", "manual", "emergency_plan",
            "scheme", "report", "case", "ledger", "other",
        }

    def test_normalize(self):
        assert taxonomy.normalize_doc_type(None) == "other"
        assert taxonomy.normalize_doc_type("乱填的") == "other"
        assert taxonomy.normalize_doc_type("emergency_plan") == "emergency_plan"

    def test_labels(self):
        assert taxonomy.doc_type_label("emergency_plan") == "预案"
        assert taxonomy.doc_type_label("ledger") == "台账"
        assert taxonomy.doc_type_label("未知") == "其他"


class TestOptions:
    def test_as_options_shape(self):
        opts = taxonomy.as_options(taxonomy.BUSINESS_DOMAINS)
        assert len(opts) == 6
        assert set(opts[0]) == {"value", "label", "description"}
