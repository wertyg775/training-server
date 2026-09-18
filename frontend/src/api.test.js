import assert from 'node:assert/strict';
import { test, afterEach } from 'node:test';
import { listReadyProjects, uploadProject } from './api.js';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

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
