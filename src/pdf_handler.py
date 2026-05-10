"""PDF 처리 모듈"""

from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
try:
    from PIL import Image
    from pdf2image import convert_from_path
    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False

from config import get_config
from logger import setup_logger


class PDFHandler:
    """PDF 처리 클래스"""

    def __init__(self):
        """PDF 핸들러를 초기화합니다."""
        self.config = get_config()
        self.logger = setup_logger(__name__)
        self.pdf_dir = Path(self.config.get_download_config().get('pdf_directory', 'artifacts/pdfs'))
        self.ocr_ready_dir = Path(self.config.get_download_config().get('ocr_ready_directory', 'artifacts/ocr_ready'))
        
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.ocr_ready_dir.mkdir(parents=True, exist_ok=True)
        
        self.ocr_config = self.config.get_ocr_config()

    def get_pdf_info(self, pdf_filename: str) -> Optional[Dict[str, Any]]:
        """
        PDF 파일 정보를 가져옵니다.
        
        Args:
            pdf_filename: PDF 파일명
            
        Returns:
            파일 정보 또는 None
        """
        pdf_path = Path(pdf_filename)
        if not pdf_path.is_absolute():
            pdf_path = self.pdf_dir / pdf_path
        
        if not pdf_path.exists():
            self.logger.warning(f"PDF 파일을 찾을 수 없습니다: {pdf_filename}")
            return None
        
        try:
            file_size = pdf_path.stat().st_size
            return {
                'filename': pdf_filename,
                'path': str(pdf_path),
                'size_bytes': file_size,
                'size_mb': file_size / (1024 * 1024),
                'created_date': datetime.fromtimestamp(pdf_path.stat().st_ctime).isoformat(),
                'modified_date': datetime.fromtimestamp(pdf_path.stat().st_mtime).isoformat(),
            }
        except Exception as e:
            self.logger.error(f"PDF 정보 조회 오류: {e}")
            return None

    def prepare_for_ocr(self, pdf_filename: str) -> bool:
        """
        PDF를 OCR 준비 상태로 변환합니다.
        (PDF를 이미지로 변환)
        
        Args:
            pdf_filename: PDF 파일명
            
        Returns:
            성공 여부
        """
        if not self.ocr_config.get('enabled', True):
            self.logger.info("OCR 기능이 비활성화되었습니다.")
            return False
        
        if not HAS_PDF2IMAGE:
            self.logger.warning("pdf2image 라이브러리가 설치되지 않았습니다.")
            return False
        
        pdf_path = self.pdf_dir / pdf_filename
        
        if not pdf_path.exists():
            self.logger.error(f"PDF 파일을 찾을 수 없습니다: {pdf_filename}")
            return False
        
        try:
            # PDF를 이미지로 변환
            relative_name = str(Path(pdf_filename).with_suffix('')).replace('/', '_')
            output_dir = self.ocr_ready_dir / relative_name
            output_dir.mkdir(parents=True, exist_ok=True)
            
            images = convert_from_path(
                str(pdf_path),
                dpi=self.ocr_config.get('dpi', 300),
            )
            
            # 이미지 저장
            image_format = self.ocr_config.get('image_format', 'png').upper()
            for i, image in enumerate(images):
                output_file = output_dir / f"page_{i+1:03d}.{image_format.lower()}"
                image.save(str(output_file), format=image_format, quality=self.ocr_config.get('quality', 95))
            
            self.logger.info(f"OCR 준비 완료: {pdf_filename} ({len(images)}페이지)")
            
            # 메타데이터 저장
            if self.ocr_config.get('status_tracking', True):
                metadata = {
                    'pdf_filename': pdf_filename,
                    'pages': len(images),
                    'output_dir': str(output_dir),
                    'format': image_format,
                    'dpi': self.ocr_config.get('dpi', 300),
                    'converted_date': datetime.now().isoformat(),
                }
                
                metadata_file = output_dir / 'ocr_metadata.json'
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
            
            return True
            
        except Exception as e:
            self.logger.error(f"OCR 준비 오류: {e}")
            return False

    def prepare_batch_for_ocr(self, pdf_filenames: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        배치로 PDF를 OCR 준비 상태로 변환합니다.
        
        Args:
            pdf_filenames: PDF 파일명 리스트 (None이면 모든 PDF)
            
        Returns:
            처리 결과
        """
        if pdf_filenames is None:
            pdf_filenames = [str(f.relative_to(self.pdf_dir)) for f in self.pdf_dir.rglob('*.pdf')]
        
        results = {
            'total': len(pdf_filenames),
            'successful': 0,
            'failed': 0,
            'skipped': 0,
        }
        
        for filename in pdf_filenames:
            if self.prepare_for_ocr(filename):
                results['successful'] += 1
            else:
                results['failed'] += 1
        
        return results

    def get_ocr_status(self) -> Dict[str, Any]:
        """OCR 준비 상태를 조회합니다."""
        ocr_items = list(self.ocr_ready_dir.glob('*'))
        
        status = {
            'total_ocr_ready': len(ocr_items),
            'items': [],
        }
        
        for item_dir in ocr_items:
            if item_dir.is_dir():
                images = list(item_dir.glob(f'*.{self.ocr_config.get("image_format", "png")}'))
                status['items'].append({
                    'name': item_dir.name,
                    'pages': len(images),
                    'path': str(item_dir),
                })
        
        return status

    def cleanup_old_ocr_data(self, days: int = 30) -> int:
        """
        오래된 OCR 데이터를 정리합니다.
        
        Args:
            days: 지정된 일수보다 오래된 데이터 삭제
            
        Returns:
            삭제된 항목 수
        """
        from datetime import datetime, timedelta
        import shutil
        
        deleted_count = 0
        cutoff_time = datetime.now() - timedelta(days=days)
        
        for item_dir in self.ocr_ready_dir.glob('*'):
            if item_dir.is_dir():
                mtime = datetime.fromtimestamp(item_dir.stat().st_mtime)
                if mtime < cutoff_time:
                    try:
                        shutil.rmtree(item_dir)
                        deleted_count += 1
                    except Exception as e:
                        self.logger.error(f"OCR 데이터 삭제 오류: {e}")
        
        self.logger.info(f"오래된 OCR 데이터 정리: {deleted_count}개 삭제")
        return deleted_count
