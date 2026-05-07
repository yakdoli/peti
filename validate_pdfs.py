#!/usr/bin/env python3
"""
PDF 무결성 검증 실행 스크립트
"""

import sys
from pathlib import Path

# 프로젝트 경로 추가
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / 'src'))

from pdf_validator import PDFValidator

if __name__ == '__main__':
    validator = PDFValidator("data/pdfs")
    summary = validator.validate_all_pdfs()
    
    print("\n💾 검증 보고서 저장 중...")
    if validator.save_report():
        print("✅ 보고서 저장 완료: data/validation_report.json")
    else:
        print("❌ 보고서 저장 실패")
    
    # 종합 결과
    print("\n" + "=" * 70)
    print("📊 최종 검증 결과")
    print("=" * 70)
    print(f"총 파일: {summary['summary']['total_files']}")
    print(f"통과: {summary['summary']['passed']}")
    print(f"실패: {summary['summary']['failed']}")
    print(f"성공률: {summary['summary']['pass_rate']}")
    print("=" * 70 + "\n")
