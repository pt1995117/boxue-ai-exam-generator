from admin_api import _extract_slice_images, _extract_slice_text


def test_extract_slice_text_dedups_duplicate_image_analysis():
    item = {
        "结构化内容": {
            "context_before": "前文",
            "images": [
                {"analysis": "同一段图片解析"},
                {"analysis": "同一段图片解析"},
                {"analysis": "另一段图片解析"},
            ],
            "context_after": "后文",
        }
    }

    text = _extract_slice_text(item)

    assert text.count("同一段图片解析") == 1
    assert "另一段图片解析" in text
    assert "前文" in text and "后文" in text


def test_extract_slice_images_dedups_same_image_triplet():
    item = {
        "结构化内容": {
            "images": [
                {
                    "image_id": "image8.png",
                    "image_path": "data/sh/slices/images/vx/image8.png",
                    "analysis": "图像解析A",
                    "contains_table": False,
                    "contains_chart": True,
                },
                {
                    "image_id": "image8.png",
                    "image_path": "data/sh/slices/images/vx/image8.png",
                    "analysis": "图像解析A",
                    "contains_table": False,
                    "contains_chart": True,
                },
            ]
        }
    }

    images = _extract_slice_images(item)

    assert len(images) == 1
    assert images[0]["image_id"] == "image8.png"
