import pytest
import pgserver
import subprocess
import tempfile
from typing import Optional
import multiprocessing as mp
import shutil
import time
from pathlib import Path
import pgserver.utils
import socket
from pgserver.utils import find_suitable_port, process_is_running
import psutil
import platform

def _check_server_works(pg : pgserver.PostgresServer) -> int:
    assert pg.pgdata.exists()
    pid = pg.get_pid()
    assert pid is not None
    ret = pg.psql("show data_directory;")

    # parse second row (first two are headers)
    ret_path = Path(ret.splitlines()[2].strip())
    assert pg.pgdata == ret_path
    return pid

def _kill_server(pid : Optional[int]) -> None:
    if pid is None:
        return
    if psutil.pid_exists(pid):
        psutil.Process(pid).kill()

def test_get_port():
    address = '127.0.0.1'
    port = find_suitable_port(address)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        sock.bind((address, port))
    except OSError as err:
        if 'Address already in use' in str(err):
            raise RuntimeError(f"Port {port} is already in use.")
        raise err
    finally:
        sock.close()


def test_get_server():
    with tempfile.TemporaryDirectory() as tmpdir:
        pid = None
        try:
            # check case when initializing the pgdata dir
            with pgserver.get_server(tmpdir) as pg:
                pid = _check_server_works(pg)

            assert not process_is_running(pid)
            assert pg.pgdata.exists()

            # check case when pgdata dir is already initialized
            with pgserver.get_server(tmpdir) as pg:
                pid = _check_server_works(pg)

            assert not process_is_running(pid)
            assert pg.pgdata.exists()
        finally:
            _kill_server(pid)


def test_reentrant():
    with tempfile.TemporaryDirectory() as tmpdir:
        pid = None
        try:
            with pgserver.get_server(tmpdir) as pg:
                pid = _check_server_works(pg)
                with pgserver.get_server(tmpdir) as pg2:
                    assert pg2 is pg
                    _check_server_works(pg)

                _check_server_works(pg)

            assert not process_is_running(pid)
            assert pg.pgdata.exists()
        finally:
            _kill_server(pid)

def _start_and_wait(tmpdir, queue_in, queue_out):
    with pgserver.get_server(tmpdir) as pg:
        pid = _check_server_works(pg)
        queue_out.put(pid)

        # now wait for parent to tell us to exit
        _ = queue_in.get()

if platform.system() != 'Windows':
    """ This test is for unix socket domain path length issues,
        and relies on using /tmp/ as the temporary directory to ensure short paths
        (unlike those used by pytest).
    """
    def test_dir_length():
        long_prefix = '_'.join(['long'] + ['1234567890']*12)
        assert len(long_prefix) > 120
        prefixes = ['short', long_prefix]

        for prefix in prefixes:
            with tempfile.TemporaryDirectory(dir='/tmp/', prefix=prefix) as tmpdir:
                pid = None
                try:
                    with pgserver.get_server(tmpdir) as pg:
                        pid = _check_server_works(pg)

                    assert not process_is_running(pid)
                    assert pg.pgdata.exists()
                    if len(prefix) > 120:
                        assert str(tmpdir) not in pg.get_uri()
                    else:
                        assert str(tmpdir) in pg.get_uri()
                finally:
                    _kill_server(pid)

def test_cleanup_delete():
    with tempfile.TemporaryDirectory() as tmpdir:
        pid = None
        try:
            with pgserver.get_server(tmpdir, cleanup_mode='delete') as pg:
                pid = _check_server_works(pg)

            assert not process_is_running(pid)
            assert not pg.pgdata.exists()
        finally:
            _kill_server(pid)

def test_cleanup_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        pid = None
        try:
            with pgserver.get_server(tmpdir, cleanup_mode=None) as pg:
                pid = _check_server_works(pg)

            assert process_is_running(pid)
            assert pg.pgdata.exists()
        finally:
            _kill_server(pid)

@pytest.fixture
def tmp_postgres():
    tmp_pg_data = tempfile.mkdtemp()
    with pgserver.get_server(tmp_pg_data, cleanup_mode='delete') as pg:
        yield pg

def test_pgvector(tmp_postgres):
    ret = tmp_postgres.psql("CREATE EXTENSION vector;")
    assert ret.strip() == "CREATE EXTENSION"

def test_start_failure_log(caplog):
    """ Test server log contents are shown in python log when failures
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with pgserver.get_server(tmpdir) as _:
            pass

        ## now delete some files to make it fail
        for f in Path(tmpdir).glob('**/postgresql.conf'):
            f.unlink()

        with pytest.raises(subprocess.CalledProcessError):
            with pgserver.get_server(tmpdir) as _:
                pass

        assert 'postgres: could not access the server configuration file' in caplog.text

def _reuse_deleted_datadir(prefix):
    """ test that new server starts normally on new datadir with a re-used name,
          after datadir is deleted
    """
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    orig_pid = None
    new_pid = None
    try:
        pgdata = Path(tmpdir) / 'pgdata'
        with pgserver.get_server(pgdata, cleanup_mode=None) as pg:
            orig_pid = _check_server_works(pg)

        if platform.system() == 'Windows':
            # windows will not allow deletion of the directory while the server is running
            _kill_server(orig_pid)

        shutil.rmtree(pgdata)
        assert not pgdata.exists()

        # starting the server on same dir should work
        with pgserver.get_server(pgdata, cleanup_mode=None) as pg:
            new_pid = _check_server_works(pg)
            assert orig_pid != new_pid
    finally:
        _kill_server(orig_pid)
        _kill_server(new_pid)

    shutil.rmtree(tmpdir)

def test_no_conflict():
    """ test we can start pgservers on two different datadirs with no conflict (eg port conflict)
    """
    pid1 = None
    pid2 = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            with pgserver.get_server(tmpdir1) as pg1, pgserver.get_server(tmpdir2) as pg2:
                pid1 = _check_server_works(pg1)
                pid2 = _check_server_works(pg2)
    finally:
        _kill_server(pid1)
        _kill_server(pid2)


def test_reuse_deleted_datadir_short():
    """ test that new server starts normally on same datadir after datadir is deleted
    """
    _reuse_deleted_datadir('short_prefix')

def test_reuse_deleted_datadir_long():
    """ test that new server starts normally on same datadir after datadir is deleted
    """
    long_prefix = '_'.join(['long_prefix'] + ['1234567890']*12)
    assert len(long_prefix) > 120
    _reuse_deleted_datadir(long_prefix)

def test_multiprocess_shared():
    """ Test that multiple processes can share the same server.

        1. get server in a child process,
        2. then, get server in the parent process
        3. then, exiting the child process
        4. checking the parent can still use the server.
    """
    pid = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_to_child = mp.Queue()
            queue_from_child = mp.Queue()
            child = mp.Process(target=_start_and_wait, args=(tmpdir,queue_to_child,queue_from_child))
            child.start()
            # wait for child to start server
            server_pid_child = queue_from_child.get()

            with pgserver.get_server(tmpdir) as pg:
                server_pid_parent = _check_server_works(pg)
                assert server_pid_child == server_pid_parent

                # tell child to continue
                queue_to_child.put(None)
                child.join()

                # check server still works
                _check_server_works(pg)

            assert not process_is_running(server_pid_parent)
    finally:
        _kill_server(pid)


@pytest.mark.skip(reason="run locally only (needs dep)")
def test_uri_string(tmp_postgres):
    import sqlalchemy as sa
    engine = sa.create_engine(tmp_postgres.get_uri('mydb'))
    conn = engine.connect()
    with conn.begin():
        conn.execute(sa.text("create table foo (id int);"))
        conn.execute(sa.text("insert into foo values (1);"))
        cur = conn.execute(sa.text("select * from foo;"))
        assert cur.fetchone()[0] == 1

@pytest.mark.skip(reason="not yet implemented")
def test_delete_pgdata_cleanup(tmp_postgres):
    """ Test server process is stopped when pgdata is deleted.
    """
    assert tmp_postgres.pgdata.exists()
    pid =  tmp_postgres.get_pid()
    assert pid is not None
    assert process_is_running(pid)

    # external deletion of pgdata should stop server
    shutil.rmtree(tmp_postgres.pgdata)

    # wait for server to stop
    for _ in range(20): # wait at most 3 seconds.
        time.sleep(.2)
        if not process_is_running(pid):
            break

    assert not process_is_running(pid)
