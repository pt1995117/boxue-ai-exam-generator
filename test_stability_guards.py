import exam_graph


def test_calc_target_lock_restores_baseline_stem_on_target_drift():
    baseline = {
        "题干": "根据规则计算本次应缴税额（　）。",
        "选项1": "1000元",
        "选项2": "2000元",
        "选项3": "3000元",
        "选项4": "4000元",
        "解析": "按规则计算税额，单位为元。",
    }
    drifted = {
        "题干": "根据规则计算本次应税面积（　）。",
        "选项1": "10平方米",
        "选项2": "20平方米",
        "选项3": "30平方米",
        "选项4": "40平方米",
        "解析": "按规则计算面积，单位为平方米。",
    }
    fixed, notices = exam_graph._enforce_calc_target_lock_on_final_json(
        drifted,
        baseline_json=baseline,
        expected_target="税额",
    )
    assert fixed["题干"] == baseline["题干"]
    assert any("目标语义漂移回退" in x for x in notices)


def test_detect_text_pollution_issue_detects_repeated_about():
    polluted = "关于关于关于本题规则，关于关于表述如下。"
    assert exam_graph._detect_text_pollution_issue(polluted) != ""
