import assert from 'node:assert/strict';
import { test, afterEach } from 'node:test';
import { listProjectFiles, listReadyProjects, readProjectFile, submitTraining, uploadProject } from './api.js';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('submits the selected script and epochs as a training request', async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/project-id/training-jobs');
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(options.body), { entrypoint: 'src/train.py', epochs: 12 });
    return Response.json({ id: 'job-id' }, { status: 201 });
  };
  assert.equal((await submitTraining('project-id', 'src/train.py', 12)).id, 'job-id');
});

test('surfaces rejected training requests', async () => {
  globalThis.fetch = async () => Response.json({ detail: 'Project is not ready.' }, { status: 409 });
  await assert.rejects(submitTraining('project-id', 'train.py', 1), /Project is not ready/);
});

test('loads file contents with encoded paths and cancellation', async () => {
  const controller = new AbortController();
  const content = '  print("hello")\n\n';
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/project-id/file?path=src%2Fmy+file%23.py');
    assert.equal(options.signal, controller.signal);
    return Response.json({ path: 'src/my file#.py', content });
  };
  assert.equal((await readProjectFile('project-id', 'src/my file#.py', controller.signal)).content, content);
  globalThis.fetch = async () => Response.json({ detail: 'Binary files cannot be previewed.' }, { status: 400 });
  await assert.rejects(readProjectFile('project-id', 'binary'), /Binary files/);
});

test('loads root and nested directories with encoded paths and cancellation', async () => {
  const controller = new AbortController();
  const paths = [];
  globalThis.fetch = async (url, options) => {
    paths.push(url);
    assert.equal(options.signal, controller.signal);
    return Response.json({ path: '', entries: [{ name: 'src', path: 'src', type: 'directory' }] });
  };
  assert.equal((await listProjectFiles('project-id', '', controller.signal)).entries[0].name, 'src');
  await listProjectFiles('project-id', 'src/data & models', controller.signal);
  assert.deepEqual(paths, ['/api/projects/project-id/files', '/api/projects/project-id/files?path=src%2Fdata+%26+models']);
});

test('reports directory errors from the backend', async () => {
  globalThis.fetch = async () => Response.json({ detail: 'Project is not ready.' }, { status: 409 });
  await assert.rejects(listProjectFiles('project-id'), /Project is not ready/);
});

test('lists only ready projects and preserves server ordering', async () => {
  globalThis.fetch = async (url) => {
    assert.equal(url, '/api/projects/ready');
    return Response.json([{ id: 'new', status: 'ready' }, { id: 'failed', status: 'failed' }, { id: 'old', status: 'ready' }]);
  };
  assert.deepEqual((await listReadyProjects()).map((project) => project.id), ['new', 'old']);
});

test('uploads ZIPs using the backend multipart field names', async () => {
  const file = new File(['zip contents'], 'source.zip', { type: 'application/zip' });
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/upload');
    assert.equal(options.method, 'POST');
    assert.equal(options.body.get('name'), 'Example');
    assert.equal(options.body.get('file').name, 'source.zip');
    assert.equal(await options.body.get('file').text(), 'zip contents');
    assert.equal(options.headers, undefined);
    return Response.json({ id: 'uploaded', status: 'ready' }, { status: 201 });
  };
  assert.equal((await uploadProject('Example', file)).id, 'uploaded');
});

test('reports import failures and non-JSON server errors', async () => {
  globalThis.fetch = async () => Response.json({ error: 'Invalid ZIP archive.' }, { status: 400 });
  await assert.rejects(uploadProject('Example', new File([], 'bad.zip')), /Invalid ZIP/);
  globalThis.fetch = async () => new Response('Bad gateway', { status: 502 });
  await assert.rejects(listReadyProjects(), /Request failed \(502\)/);
});
