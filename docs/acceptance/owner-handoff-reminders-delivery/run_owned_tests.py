"""仅精确 owned 临时 PG；凭证与原始失败输出不离开进程。"""
import os, secrets, subprocess, sys, time, uuid
import docker
client = None
container = None
owner = uuid.uuid4().hex
try:
    client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
    image = client.images.get('pgvector/pgvector:pg16')
    password = secrets.token_hex(24)
    container = client.containers.run(image.id, detach=True, name='handoff-test-'+owner, labels={'tradeos.handoff-test-owner':owner}, environment={'POSTGRES_USER':'handoff_test','POSTGRES_PASSWORD':password,'POSTGRES_DB':'handoff_test'}, ports={'5432/tcp':('127.0.0.1',None)}, remove=False)
    for attempt in range(100):
        if container.exec_run(['pg_isready','-U','handoff_test','-d','handoff_test']).exit_code == 0:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('database readiness failed')
    container.reload()
    port = container.attrs['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
    dsn = f'postgresql+asyncpg://handoff_test:{password}@127.0.0.1:{port}/handoff_test'
    env = dict(os.environ, PYTHON_DOTENV_DISABLED='1', DATABASE_URL=dsn, TEST_DATABASE_URL=dsn, DOCKER_HOST='unix:///var/run/docker.sock')
    migrate = subprocess.run([sys.executable,'scripts/run_alembic.py','upgrade','head'],env=env,capture_output=True)
    if migrate.returncode:
        raise RuntimeError('migration failed')
    result = subprocess.run([sys.executable,'-m','pytest',*sys.argv[1:],'-q','--tb=short'],env=env,capture_output=True,text=True)
    for line in result.stdout.splitlines():
        if line.startswith('tests/') and ': in ' in line:
            print(line)
        if line.startswith('E   ') and ':' in line:
            print(line.split(':',1)[0])
            continue
        if line.startswith(('FAILED ', 'PASSED ', 'ERROR ', 'E   TypeError:', 'E   AttributeError:', 'E   AssertionError:', 'E   assert ', 'E   shared.errors.', 'E   domains.')) or ' passed' in line or ' failed' in line or ' error' in line or ' skipped' in line:
            if password not in line and '://' not in line:
                print(line)
    print('pytest exit:',result.returncode)
    sys.exit(result.returncode)
except SystemExit:
    raise
except BaseException as error:
    print('owned runner failure category:',type(error).__name__)
    sys.exit(2)
finally:
    if container is not None:
        container.reload()
        if container.labels.get('tradeos.handoff-test-owner') == owner:
            container.remove(force=True,v=True)
    if client is not None:
        client.close()
