#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试图片处理功能（单张图片）
"""
import sys
import os

def main():
    if len(sys.argv) < 2:
        print("用法: python3 test_image_processing.py <图片路径>")
        return
    
    image_path = sys.argv[1]
    if not os.path.isfile(image_path):
        print(f"❌ 图片文件不存在: {image_path}")
        return
    
    # 导入处理函数
    from process_textbook_images import load_config, analyze_image_with_qwen_vl
    
    config = load_config()
    api_key = config.get('CRITIC_API_KEY') or config.get('OPENAI_API_KEY') or ''
    
    if not api_key:
        print("❌ 未找到 API Key")
        return
    
    print(f"分析图片: {image_path}")
    print("="*70)
    
    result = analyze_image_with_qwen_vl(image_path, api_key)
    
    if result:
        print("\n分析结果:")
        print("="*70)
        print(result)
        print("="*70)
    else:
        print("❌ 分析失败")

if __name__ == '__main__':
    main()
