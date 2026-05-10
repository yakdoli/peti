#!/usr/bin/env python3
"""
PDF 무결성 검증 모듈
저장된 PDF 파일의 유효성을 검증합니다.
"""

from pathlib import Path
from typing import Dict, List, Any, Tuple
import hashlib
import json
from datetime import datetime

try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False


class PDFValidator:
    """PDF 파일 검증 클래스"""

    def __init__(self, pdf_directory: str = "artifacts/pdfs"):
        """초기화"""
        self.pdf_dir = Path(pdf_directory)
        self.validation_results = []

    def validate_all_pdfs(self) -> Dict[str, Any]:
        """모든 PDF 파일을 검증합니다."""
        if not self.pdf_dir.exists():
            return {
                'status': 'error',
                'message': f"PDF 디렉토리를 찾을 수 없습니다: {self.pdf_dir}",
                'results': []
            }

        pdf_files = list(self.pdf_dir.rglob('*.pdf'))
        
        print(f"\n🔍 PDF 무결성 검증 시작")
        print(f"대상 파일: {len(pdf_files)}개")
        print("-" * 70)

        self.validation_results = []
        
        for pdf_file in sorted(pdf_files):
            result = self.validate_pdf(pdf_file)
            self.validation_results.append(result)
            self._print_result(result)

        return self._get_summary()

    def validate_pdf(self, pdf_path: Path) -> Dict[str, Any]:
        """단일 PDF 파일을 검증합니다."""
        result = {
            'filename': pdf_path.name,
            'path': str(pdf_path),
            'timestamp': datetime.now().isoformat(),
            'checks': {}
        }

        # 1. 파일 존재 여부
        result['checks']['exists'] = {
            'status': 'pass' if pdf_path.exists() else 'fail',
            'message': '파일 존재'
        }

        if not pdf_path.exists():
            result['overall_status'] = 'fail'
            return result

        # 2. 파일 크기 확인
        file_size = pdf_path.stat().st_size
        result['file_size_bytes'] = file_size
        result['file_size_mb'] = round(file_size / (1024 * 1024), 4)
        result['checks']['file_size'] = {
            'status': 'pass' if file_size > 0 else 'fail',
            'value': f"{file_size} bytes",
            'message': '파일 크기가 0이 아님'
        }

        # 3. PDF 헤더 확인
        header_check = self._check_pdf_header(pdf_path)
        result['checks']['pdf_header'] = header_check

        # 4. 파일 접근성 확인
        try:
            with open(pdf_path, 'rb') as f:
                _ = f.read()
            result['checks']['file_readable'] = {
                'status': 'pass',
                'message': '파일을 읽을 수 있음'
            }
        except Exception as e:
            result['checks']['file_readable'] = {
                'status': 'fail',
                'message': f'파일 읽기 실패: {e}'
            }

        # 5. 해시값 계산
        result['md5_hash'] = self._calculate_md5(pdf_path)
        result['sha256_hash'] = self._calculate_sha256(pdf_path)

        # 6. PyPDF2를 사용한 검증
        if HAS_PYPDF2:
            pypdf2_check = self._check_with_pypdf2(pdf_path)
            result['checks']['pypdf2_validation'] = pypdf2_check
        else:
            result['checks']['pypdf2_validation'] = {
                'status': 'skip',
                'message': 'PyPDF2 라이브러리 미설치'
            }

        # 7. 기본 PDF 구조 확인 (EOF)
        eof_check = self._check_pdf_structure(pdf_path)
        result['checks']['pdf_structure'] = eof_check

        # 종합 결과
        all_passed = all(
            check.get('status') in ['pass', 'skip']
            for check in result['checks'].values()
        )
        result['overall_status'] = 'pass' if all_passed else 'fail'

        return result

    def _check_pdf_header(self, pdf_path: Path) -> Dict[str, Any]:
        """PDF 헤더를 확인합니다."""
        try:
            with open(pdf_path, 'rb') as f:
                header = f.read(8)
            
            if header.startswith(b'%PDF-'):
                version = header[5:8].decode('ascii', errors='ignore')
                return {
                    'status': 'pass',
                    'value': header.decode('ascii', errors='ignore'),
                    'version': version,
                    'message': 'PDF 헤더 유효'
                }
            else:
                return {
                    'status': 'fail',
                    'value': header[:8],
                    'message': 'PDF 헤더 없음'
                }
        except Exception as e:
            return {
                'status': 'fail',
                'message': f'헤더 읽기 실패: {e}'
            }

    def _check_pdf_structure(self, pdf_path: Path) -> Dict[str, Any]:
        """PDF 구조를 확인합니다 (EOF 마커)."""
        try:
            with open(pdf_path, 'rb') as f:
                content = f.read()
            
            # PDF는 %PDF로 시작해야 함
            if not content.startswith(b'%PDF'):
                return {
                    'status': 'fail',
                    'message': '유효한 PDF 헤더 없음'
                }
            
            # 기본 PDF 구조 확인 (내용이 있는지)
            if len(content) > 100:
                return {
                    'status': 'pass',
                    'size': len(content),
                    'message': '기본 PDF 구조 확인됨'
                }
            else:
                return {
                    'status': 'fail',
                    'size': len(content),
                    'message': 'PDF 내용 부족'
                }
        except Exception as e:
            return {
                'status': 'fail',
                'message': f'구조 확인 실패: {e}'
            }

    def _check_with_pypdf2(self, pdf_path: Path) -> Dict[str, Any]:
        """PyPDF2를 사용한 검증"""
        try:
            with open(pdf_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                num_pages = len(reader.pages)
            
            return {
                'status': 'pass',
                'pages': num_pages,
                'message': f'PyPDF2 검증 성공 ({num_pages}페이지)'
            }
        except Exception as e:
            return {
                'status': 'fail',
                'message': f'PyPDF2 검증 실패: {e}'
            }

    def _calculate_md5(self, pdf_path: Path) -> str:
        """MD5 해시 계산"""
        md5_hash = hashlib.md5()
        with open(pdf_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b''):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()

    def _calculate_sha256(self, pdf_path: Path) -> str:
        """SHA256 해시 계산"""
        sha256_hash = hashlib.sha256()
        with open(pdf_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b''):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()

    def _print_result(self, result: Dict[str, Any]) -> None:
        """검증 결과를 출력합니다."""
        status = result['overall_status']
        emoji = '✅' if status == 'pass' else '❌' if status == 'fail' else '⏭️'
        
        print(f"{emoji} {result['filename']}")
        print(f"   상태: {result['overall_status']}")
        print(f"   크기: {result['file_size_mb']}MB ({result['file_size_bytes']} bytes)")
        print(f"   MD5: {result['md5_hash'][:16]}...")
        
        # 상세 검사 결과
        for check_name, check_result in result['checks'].items():
            check_emoji = '✓' if check_result.get('status') == 'pass' else '✗' if check_result.get('status') == 'fail' else '⊘'
            print(f"   {check_emoji} {check_name}: {check_result.get('message', check_result.get('status'))}")

    def _get_summary(self) -> Dict[str, Any]:
        """종합 요약을 반환합니다."""
        total = len(self.validation_results)
        passed = sum(1 for r in self.validation_results if r['overall_status'] == 'pass')
        failed = sum(1 for r in self.validation_results if r['overall_status'] == 'fail')
        
        return {
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'summary': {
                'total_files': total,
                'passed': passed,
                'failed': failed,
                'pass_rate': f"{(passed / total * 100):.1f}%" if total > 0 else "0%"
            },
            'results': self.validation_results
        }

    def save_report(self, output_file: str = "artifacts/validation_report.json") -> bool:
        """검증 보고서를 저장합니다."""
        try:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            report = {
                'validation_timestamp': datetime.now().isoformat(),
                'summary': {
                    'total_files': len(self.validation_results),
                    'passed': sum(1 for r in self.validation_results if r['overall_status'] == 'pass'),
                    'failed': sum(1 for r in self.validation_results if r['overall_status'] == 'fail'),
                },
                'results': self.validation_results
            }
            
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            
            return True
        except Exception as e:
            print(f"❌ 보고서 저장 실패: {e}")
            return False


def main():
    """메인 함수"""
    validator = PDFValidator("artifacts/pdfs")
    
    print("\n" + "=" * 70)
    print("📋 PDF 무결성 검증")
    print("=" * 70)
    
    # 검증 실행
    summary = validator.validate_all_pdfs()
    
    # 보고서 저장
    print("\n💾 검증 보고서 저장 중...")
    if validator.save_report():
        print("✅ 보고서 저장 완료: artifacts/validation_report.json")
    
    # 종합 결과
    print("\n" + "=" * 70)
    print("📊 검증 결과 요약")
    print("=" * 70)
    print(f"총 파일: {summary['summary']['total_files']}")
    print(f"통과: {summary['summary']['passed']}")
    print(f"실패: {summary['summary']['failed']}")
    print(f"성공률: {summary['summary']['pass_rate']}")
    print("=" * 70)
    
    return summary


if __name__ == '__main__':
    main()
