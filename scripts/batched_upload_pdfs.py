#!/usr/bin/env python3
"""PDF 다운로드 실패 항목 리포트 및 재시도 목록 생성"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Any

import click
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.logger import setup_logger


logger = setup_logger(__name__)


class PDFFailureReporter:
    """PDF 다운로드 실패 리포터"""

    def __init__(self, data_dir: str = 'data/searchThema'):
        self.data_dir = Path(data_dir)
        self.failures_file = self.data_dir / 'pdf_failures.jsonl'

    def load_failures(self) -> List[Dict[str, Any]]:
        """실패 로그 로드"""
        if not self.failures_file.exists():
            logger.warning(f"실패 로그 파일 없음: {self.failures_file}")
            return []

        failures = []
        with open(self.failures_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    failures.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return failures

    def group_by_error(self, failures: List[Dict[str, Any]]) -> Dict[str, int]:
        """에러별로 그룹화"""
        error_counts = {}
        for failure in failures:
            error = failure.get('error', 'unknown')
            error_counts[error] = error_counts.get(error, 0) + 1
        return error_counts

    def report(self) -> None:
        """리포트 출력"""
        failures = self.load_failures()
        if not failures:
            logger.info("다운로드 실패 항목이 없습니다.")
            return

        logger.info(f"총 실패 항목: {len(failures)}개")

        error_counts = self.group_by_error(failures)
        logger.info("에러 분류:")
        for error, count in sorted(error_counts.items(), key=lambda x: -x[1])[:10]:
            logger.info(f"  {error}: {count}개")

        # 재시도를 위한 목록 생성
        retry_list_file = self.data_dir / 'pdf_retry_list.jsonl'
        with open(retry_list_file, 'w', encoding='utf-8') as f:
            for failure in failures:
                retry_item = {
                    'item_id': failure.get('item_id'),
                    'viewer_path': failure.get('viewer_path'),
                    'content_id': failure.get('content_id'),
                    'toc_id': failure.get('toc_id'),
                }
                f.write(json.dumps(retry_item, ensure_ascii=False) + '\n')

        logger.info(f"재시도 목록 생성: {retry_list_file}")


@click.command()
@click.option('--data-dir', default='data/searchThema', help='데이터 디렉토리')
def main(data_dir: str) -> None:
    """PDF 다운로드 실패 리포트"""
    logger.info("PDF 다운로드 실패 리포트 시작")
    reporter = PDFFailureReporter(data_dir)
    reporter.report()
    logger.info("리포트 완료")


if __name__ == '__main__':
    main()
