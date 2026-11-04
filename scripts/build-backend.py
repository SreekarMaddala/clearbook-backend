"""Package the three dependency-free Python Lambda handlers without Docker."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent.parent
FILES = ('api.py', 'access.py', 'provider.py', 'database.py', 'service.py', 'reference_handler.py',
         'transactions_handler.py', 'dashboard_handler.py')


def build():
    target = ROOT / '.local' / 'deploy'
    target.mkdir(parents=True, exist_ok=True)
    archive = target / 'clearbook-lambda.zip'
    with ZipFile(archive, 'w', ZIP_DEFLATED) as output:
        for name in FILES:
            source = ROOT / 'backend' / name
            compile(source.read_text(encoding='utf-8'), name, 'exec')
            output.write(source, name)
    template = (ROOT / 'template.yaml').read_text(encoding='utf-8')
    if template.count('CodeUri: backend/') != 1:
        raise RuntimeError('Expected one shared Lambda source configuration.')
    (target / 'template.yaml').write_text(template.replace('CodeUri: backend/', 'CodeUri: clearbook-lambda.zip'), encoding='utf-8')
    print(f'Lambda archive: {archive}')
    print(f'Deployment template: {target / "template.yaml"}')


if __name__ == '__main__':
    build()
