#!/usr/bin/env python3
"""SearchThema 메타데이터 JSON → DuckDB 변환 스크립트"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Any

import click

try:
    import duckdb
except ImportError:
    print("Error: 'duckdb' 패키지가 필요합니다. pip install duckdb")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.logger import setup_logger


logger = setup_logger(__name__)

CATEGORIES = [
    "감사원", "고시", "공고", "국가인권위원회", "국회", "기타",
    "대통령령", "대통령지시사항", "법률", "법원", "부령", "상훈",
    "선거관리위원회", "조달관보", "조약", "지방자치단체", "총리령",
    "헌법재판소", "훈령"
]


class MetadataToDuckDB:
    """메타데이터 JSON → DuckDB 변환"""

    def __init__(self, data_dir: str = 'data/searchThema'):
        self.data_dir = Path(data_dir)
        self.db_file = self.data_dir / 'metadata.duckdb'
        self.conn = duckdb.connect(str(self.db_file))

    def load_metadata_items(self, category: str) -> List[Dict[str, Any]]:
        """카테고리 메타데이터 로드"""
        metadata_file = self.data_dir / f'metadata_{category}.json'
        if not metadata_file.exists():
            logger.warning(f"메타데이터 파일 없음: {metadata_file}")
            return []

        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"메타데이터 로드 오류 ({metadata_file}): {e}")
            return []

    def flatten_item(self, item: Dict[str, Any], category: str) -> Dict[str, Any]:
        """아이템을 평탄화"""
        return {
            'id': item.get('id') or item.get('stored_toc_seq'),
            'category': category,
            'title': item.get('title') or item.get('stored_field_subject'),
            'date': item.get('date') or item.get('stored_field_month'),
            'agency': item.get('agency') or item.get('stored_organ_nm'),
            'pdf_status': str(item.get('pdf', {}).get('status', 'unknown')),
            'ocr_status': str(item.get('ocr', {}).get('status', 'unknown')),
            'metadata_json': json.dumps(item, ensure_ascii=False),
        }

    def convert_all_categories(self, dry_run: bool = False) -> None:
        """모든 카테고리 변환"""
        if not dry_run:
            # 기존 테이블 삭제
            try:
                self.conn.execute('DROP TABLE IF EXISTS metadata')
            except Exception:
                pass

            # 새 테이블 생성
            self.conn.execute('''
                CREATE TABLE metadata (
                    id VARCHAR PRIMARY KEY,
                    category VARCHAR NOT NULL,
                    title VARCHAR,
                    date VARCHAR,
                    agency VARCHAR,
                    pdf_status VARCHAR,
                    ocr_status VARCHAR,
                    metadata_json VARCHAR
                )
            ''')
            logger.info("테이블 생성: metadata")

        total_items = 0
        for category in CATEGORIES:
            items = self.load_metadata_items(category)
            if not items:
                continue

            flat_items = [self.flatten_item(item, category) for item in items]
            total_items += len(flat_items)

            if dry_run:
                logger.info(f"[DRY RUN] {category}: {len(flat_items)}개 항목")
            else:
                try:
                    self.conn.insert_all([
                        [item['id'], item['category'], item['title'], item['date'],
                         item['agency'], item['pdf_status'], item['ocr_status'], item['metadata_json']]
                        for item in flat_items
                    ], table_name='metadata')
                    logger.info(f"삽입 완료: {category} ({len(flat_items)}개)")
                except Exception as e:
                    logger.error(f"삽입 오류 ({category}): {e}")

        if not dry_run:
            self.conn.commit()
            logger.info(f"변환 완료: {total_items}개 항목 → {self.db_file}")

    def close(self) -> None:
        """데이터베이스 종료"""
        if self.conn:
            self.conn.close()


@click.command()
@click.option('--data-dir', default='data/searchThema', help='데이터 디렉토리')
@click.option('--dry-run', is_flag=True, help='드라이런 모드')
def main(data_dir: str, dry_run: bool) -> None:
    """SearchThema 메타데이터 JSON → DuckDB 변환"""
    logger.info("메타데이터 변환 시작")
    logger.info(f"  데이터 디렉토리: {data_dir}")
    logger.info(f"  드라이런: {dry_run}")

    converter = MetadataToDuckDB(data_dir)
    try:
        converter.convert_all_categories(dry_run)
    finally:
        converter.close()

    logger.info("변환 종료")


if __name__ == '__main__':
    main()
