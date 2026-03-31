"""
Phase 1: Deterministic Filter - 正则与脚本硬校验层

成本最低的一层，用纯 Python 实现，直接拦截格式问题，不消耗大模型 Token。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from src.schemas.evaluation import QuestionInput


@dataclass
class FilterResult:
    """过滤器校验结果。"""

    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DeterministicFilter:
    """确定性过滤器。"""

    FORBIDDEN_OPTION_PATTERNS = [
        r"以上皆是",
        r"以上皆非",
        r"以上都对",
        r"以上都错",
        r"全部正确",
        r"全部错误",
    ]
    FORBIDDEN_STEM_PATTERNS = [
        r"最重要",
        r"实实在在",
        r"不是不",
        r"并非不",
    ]
    AI_HALLUCINATION_WORDS = ["外接", "上交"]
    REDUNDANT_SCENES = ["师傅告诉徒弟", "新人培训", "通过中介买了房"]
    FORBIDDEN_NAME_PATTERNS = [r"(先生|女士)", r"张三", r"李四", r"贾董事"]
    OPTION_ENDING_PUNCTUATION = re.compile(r"[。，、；：！？.!?,;:!?]$")
    CITY_LIST = ["北京", "上海", "广州", "深圳", "杭州", "天津", "重庆", "成都", "南京"]

    def __init__(self, strict_mode: bool = True):
        self.strict_mode = strict_mode

    @staticmethod
    def _merged_slice_text(question: QuestionInput) -> str:
        """合并主教材切片与关联切片，供切片相关规则统一检索。"""
        parts = [str(question.textbook_slice or "").strip()]
        parts.extend(str(x or "").strip() for x in (question.related_slices or []))
        return "\n".join([p for p in parts if p])

    def run(self, question: QuestionInput) -> FilterResult:
        result = FilterResult()
        errors: list[str] = []
        warnings: list[str] = []

        self._check_brackets(question, errors)
        self._check_punctuation(question, errors)
        self._check_options(question, errors)
        self._check_option_hierarchy_conflict(question, warnings)
        self._check_forbidden_words(question, errors)
        self._check_numeric_option_order(question, warnings)
        self._check_realism(question, errors, warnings)
        self._check_geo_consistency(question, warnings)
        self._check_explanation(question, errors)
        self._check_leakage(question, warnings)

        result.errors = errors
        result.warnings = warnings
        result.passed = len(errors) == 0
        return result

    def _check_brackets(self, question: QuestionInput, errors: list[str]) -> None:
        stem = question.stem
        is_true_false = question.question_type == "true_false"

        if re.search(r"[()]", stem) and ("（" not in stem or "）" not in stem):
            errors.append("应使用全角中文括号（ ）而非半角 ()")

        pairs = list(re.finditer(r"[（(]([^）)]*)[）)]", stem))
        # 规则：仅“最后一个空白括号”视为作答占位并校验空格；其他括号均当作说明括号
        blank_indices = [i for i, m in enumerate(pairs) if m.group(1).strip() == ""]
        answer_blank_idx = blank_indices[-1] if blank_indices else None

        # 选择/多选题必须有且仅有一个作答占位（ ）
        if question.question_type in {"single_choice", "multiple_choice"}:
            if len(blank_indices) == 0:
                errors.append("选择题题干缺少作答占位括号（ ）")
            elif len(blank_indices) > 1:
                errors.append("选择题题干作答占位括号（ ）只能出现一次")

        # Full-width space (U+3000) only: stem ending bracket must have exactly one 全角空格 inside
        FULL_WIDTH_SPACE = "\u3000"
        for i, m in enumerate(pairs):
            inner = m.group(1)
            if i == answer_blank_idx:
                if inner != FULL_WIDTH_SPACE:
                    errors.append("题干结尾括号中间必须有且仅有一个全角空格（不能多）")

                # 作答括号不能位于句首
                prefix = stem[: m.start()].strip()
                if not prefix:
                    errors.append("作答占位括号（ ）不能放在句首")

        if is_true_false:
            if "。（ ）" not in stem and "。( )" not in stem:
                if "（" in stem or "(" in stem:
                    errors.append("判断题括号位置错误：应为'。（ ）'（句号在括号前）")
        else:
            if "（" in stem or "(" in stem:
                if not re.search(r"[（(]\s*[）)]\s*。", stem):
                    errors.append("选择题括号位置错误：应为'（ ）。'（句号在括号后）")

    def _check_punctuation(self, question: QuestionInput, errors: list[str]) -> None:
        texts = [question.stem, question.explanation] + question.options
        for i, text in enumerate(texts):
            # 禁用所有单引号：半角'、中文左单引号‘、中文右单引号’
            if any(ch in text for ch in ("'", "‘", "’")):
                if i == 0:
                    source = "题干"
                elif i == 1:
                    source = "解析"
                else:
                    source = f"选项{chr(65 + (i - 2))}"
                errors.append(f"{source}中使用了单引号，应统一换为双引号")

    def _check_options(self, question: QuestionInput, errors: list[str]) -> None:
        for i, opt in enumerate(question.options):
            content = re.sub(r"^[A-Da-d][\.．、]\s*", "", opt).strip()
            if content and self.OPTION_ENDING_PUNCTUATION.search(content):
                errors.append(f"选项{chr(65 + i)}结尾禁止使用标点符号")

    def _check_option_hierarchy_conflict(self, question: QuestionInput, warnings: list[str]) -> None:
        """
        单选题若同时出现上位类和下位类选项（如“存量住宅”与“二手房”），
        仅作“疑似风险”提示，最终是否多解由LLM结合题干条件与教材切片裁决。
        """
        if question.question_type != "single_choice" or len(question.options) < 2:
            return

        option_texts = [
            re.sub(r"^[A-Da-d][\.．、]\s*", "", o).strip() for o in question.options
        ]
        textbook = self._merged_slice_text(question)

        for i in range(len(option_texts)):
            for j in range(i + 1, len(option_texts)):
                a = option_texts[i]
                b = option_texts[j]
                if not a or not b or a == b:
                    continue

                # a 是父类、b 是子类
                p1 = rf"{re.escape(a)}中[^。；\n]{{0,80}}(?:称为|称作|属于|包括|包含)[^。；\n]{{0,60}}{re.escape(b)}"
                # b 是父类、a 是子类
                p2 = rf"{re.escape(b)}中[^。；\n]{{0,80}}(?:称为|称作|属于|包括|包含)[^。；\n]{{0,60}}{re.escape(a)}"
                # 兜底：X属于Y
                p3 = rf"{re.escape(a)}[^。；\n]{{0,20}}属于[^。；\n]{{0,20}}{re.escape(b)}"
                p4 = rf"{re.escape(b)}[^。；\n]{{0,20}}属于[^。；\n]{{0,20}}{re.escape(a)}"

                if re.search(p1, textbook) or re.search(p4, textbook):
                    warnings.append(
                        f"单选题选项疑似层级冲突：选项{chr(65+i)}“{a}”与选项{chr(65+j)}“{b}”为父子类关系，需结合题干判定是否多解"
                    )
                    return
                if re.search(p2, textbook) or re.search(p3, textbook):
                    warnings.append(
                        f"单选题选项疑似层级冲突：选项{chr(65+j)}“{b}”与选项{chr(65+i)}“{a}”为父子类关系，需结合题干判定是否多解"
                    )
                    return

    def _check_forbidden_words(self, question: QuestionInput, errors: list[str]) -> None:
        for opt in question.options:
            for pattern in self.FORBIDDEN_OPTION_PATTERNS:
                if re.search(pattern, opt):
                    errors.append(f"选项中出现违禁兜底表述：{pattern}")

        for pattern in self.FORBIDDEN_STEM_PATTERNS:
            if re.search(pattern, question.stem):
                errors.append(f"题干出现不规范设问/模糊词：{pattern}")

    def _check_numeric_option_order(self, question: QuestionInput, warnings: list[str]) -> None:
        numbers: list[float] = []
        for opt in question.options:
            match = re.search(r"(\d+\.?\d*)\s*(?:万|元|%|米|平米)?", opt)
            if match:
                numbers.append(float(match.group(1)))

        if len(numbers) >= 2 and len(numbers) == len(question.options):
            if numbers != sorted(numbers):
                warnings.append("数值型选项建议按从小到大升序排列")

    def _check_ask_pattern(
        self,
        question: QuestionInput,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        stem = question.stem
        normalized = re.sub(r"\s+", "", stem)

        # 问号属于硬错误（这里补充兜底；_check_forbidden_words 也会拦）
        if "?" in stem or "？" in stem:
            errors.append("设问句式不规范：应使用陈述句，禁止问号")
            return

        single_patterns = [
            r"(以下|下列).{0,8}(表述|说法|选项).{0,6}正确.{0,2}是",
            r"(以下|下列).{0,8}正确.{0,2}是",
        ]
        multiple_patterns = [
            r"(以下|下列).{0,8}(表述|说法|选项).{0,6}正确的有",
            r"(以下|下列).{0,8}(表述|说法|选项).{0,6}正确的包括",
            r"(以下|下列).{0,8}正确的有",
            r"(以下|下列).{0,8}正确的包括",
        ]

        def _match_any(patterns: list[str], text: str) -> bool:
            return any(re.search(p, text) for p in patterns)

        def _looks_like_valid_declarative_blank(text: str) -> bool:
            # 允许“业务陈述句 + 作答占位”类问法，不强制“以下/下列...”固定模板
            if "?" in text or "？" in text:
                return False
            if not re.search(r"[（(]\s*[）)]", text):
                return False
            # 句子主干中出现常见陈述谓词即可视为合格
            return bool(re.search(r"(是|为|属于|包括|应|可|可以|需要|需|在收集|用于|表示)", text))

        if question.question_type == "single_choice":
            if not _match_any(single_patterns, normalized) and not _looks_like_valid_declarative_blank(stem):
                warnings.append("单选题句式建议为“以下/下列表述（说法）正确的是（ ）。”")
        elif question.question_type == "multiple_choice":
            if not _match_any(multiple_patterns, normalized) and not _looks_like_valid_declarative_blank(stem):
                warnings.append("多选题句式建议为“以下/下列表述（说法）正确的有/包括（ ）。”")
        elif question.question_type == "true_false":
            # 判断题要出现明确结论锚点
            if "正确" not in stem and "错误" not in stem:
                errors.append("判断题题干需包含结论锚点（正确/错误）")

    def _check_realism(
        self, question: QuestionInput, errors: list[str], warnings: list[str]
    ) -> None:
        text = question.stem + "\n" + "\n".join(question.options)
        for w in self.AI_HALLUCINATION_WORDS:
            if w in text:
                warnings.append(f"检测到疑似AI幻觉词：{w}")

        for w in self.REDUNDANT_SCENES:
            if w in question.stem:
                warnings.append(f"题干存在冗余场景描述：{w}")

        for pattern in self.FORBIDDEN_NAME_PATTERNS:
            if re.search(pattern, question.stem):
                warnings.append(f"人物称谓可能不规范：{pattern}")

        m = re.search(r"得房率\s*(\d{1,3})%", question.stem)
        if m and int(m.group(1)) < 70:
            warnings.append("普通住宅得房率低于70%，疑似不符合常识")

        m = re.search(r"层高\s*(\d+(?:\.\d+)?)\s*米", question.stem)
        if m and float(m.group(1)) > 6:
            warnings.append("复式楼层高超过6米，疑似不符合常识")

        # 常识一致性硬校验：楼层数 vs 建筑高度
        # 默认以住宅单层最小合理层高2.8m作为阈值，可通过环境变量覆盖。
        min_floor_height = float(os.getenv("MIN_RESIDENTIAL_FLOOR_HEIGHT_M", "2.8"))
        max_floor_height = float(os.getenv("MAX_RESIDENTIAL_FLOOR_HEIGHT_M", "6.0"))
        floors = re.search(r"地上(?:实际)?总?层数(?:为)?\s*(\d+)\s*层|(\d+)\s*层", question.stem)
        height = re.search(r"建筑高度(?:为)?\s*(\d+(?:\.\d+)?)\s*米", question.stem)
        if floors and height:
            floors_num = int(floors.group(1) or floors.group(2))
            height_m = float(height.group(1))
            if floors_num > 0:
                avg = height_m / floors_num
                if avg < min_floor_height:
                    errors.append(
                        f"层数与建筑高度不符合常识：{floors_num}层对应{height_m}米，平均层高约{avg:.2f}米，低于{min_floor_height:.1f}米"
                    )
                elif avg > max_floor_height:
                    errors.append(
                        f"层数与建筑高度不符合常识：{floors_num}层对应{height_m}米，平均层高约{avg:.2f}米，高于{max_floor_height:.1f}米"
                    )

    def _check_geo_consistency(self, question: QuestionInput, warnings: list[str]) -> None:
        stem_cities = [c for c in self.CITY_LIST if c in question.stem]
        merged_textbook = self._merged_slice_text(question)
        book_cities = [c for c in self.CITY_LIST if c in merged_textbook]
        if stem_cities and book_cities and set(stem_cities) != set(book_cities):
            warnings.append(
                f"题干城市({','.join(stem_cities)})与教材城市({','.join(book_cities)})可能不一致"
            )

    def _check_explanation(self, question: QuestionInput, errors: list[str]) -> None:
        exp = question.explanation
        section_rules = [
            ("1.教材原文", r"(^|\n)\s*1[\.、]\s*教材原文(?:\s*[：:])?"),
            ("2.试题分析", r"(^|\n)\s*2[\.、]\s*试题分析(?:\s*[：:])?"),
            ("3.结论", r"(^|\n)\s*3[\.、]\s*结论(?:\s*[：:])?"),
        ]
        for section_name, pattern in section_rules:
            if not re.search(pattern, exp, flags=re.MULTILINE):
                errors.append(f"解析缺少结构段落：{section_name}")

        if question.question_type == "true_false":
            if not re.search(r"本题答案为\s*(正确|错误)\s*[。．.]?$", exp.strip()):
                errors.append("判断题结尾必须为“本题答案为正确/错误”")
        else:
            if not re.search(
                r"本题答案为\s*[A-D](?:\s*[、,，]\s*[A-D])*\s*[。．.]?$",
                exp.strip().upper(),
            ):
                errors.append("选择题结尾必须为“本题答案为A/B/C/D”")

    @staticmethod
    def _normalize_answer(text: str) -> str:
        t = str(text or "").strip().upper()
        t = re.sub(r"\s+", "", t)
        t = t.replace("，", ",").replace("、", ",")
        if t in {"正确", "错误"}:
            return t
        if re.fullmatch(r"[A-D](?:,[A-D])*", t):
            parts = sorted(set(t.split(",")))
            return ",".join(parts)
        return t

    def _check_leakage(self, question: QuestionInput, warnings: list[str]) -> None:
        # 判断题不做该告警：A/B 仅为“正确/错误”，不适用“题干-选项文本直给”检测。
        if question.question_type == "true_false":
            return

        # Strict phrase match: only warn when stem contains a substantial verbatim phrase
        # from the correct option (avoid keyword-overlap false positives).
        answer_idx = None
        letters = [x.strip() for x in question.correct_answer.split(",") if x.strip()]
        if len(letters) == 1 and letters[0] in ["A", "B", "C", "D"]:
            answer_idx = ord(letters[0]) - ord("A")

        if answer_idx is None or answer_idx >= len(question.options):
            return

        correct_opt = re.sub(r"^[A-Da-d][\.．、]\s*", "", question.options[answer_idx]).strip()
        if not correct_opt:
            return
        phrase_candidates = [x.strip() for x in re.split(r"[，。；：、]", correct_opt) if len(x.strip()) >= 6]
        if not phrase_candidates and len(correct_opt) >= 8:
            phrase_candidates = [correct_opt]
        if any(p in question.stem for p in phrase_candidates):
            warnings.append("题目疑似傻瓜化直给答案：题干与正确选项出现大段原样重合")
