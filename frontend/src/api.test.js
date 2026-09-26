import assert from 'node:assert/strict';
import { test, afterEach } from 'node:test';
import { listExecutions, listOutputRuns, listProjectFiles, listReadyProjects, listTrainingJobs, outputDownloadUrl, readProjectFile, submitTraining, uploadProject } from './api.js';

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

test('lists training jobs with cancellation support', async () => {
  const controller = new AbortController();
  const jobs = [{ id: 'job-1', project_name: 'Demo', entrypoint: 'train.py', status: 'queued' }];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/training-jobs');
    assert.equal(options.signal, controller.signal);
    return Response.json(jobs);
  };
  assert.deepEqual(await listTrainingJobs(controller.signal), jobs);
});

test('lists executions with cancellation support', async () => {
  const controller = new AbortController();
  const executions = [{ id: 'execution-1', project_name: 'Demo', state: 'succeeded' }];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/executions');
    assert.equal(options.signal, controller.signal);
    return Response.json(executions);
  };
  assert.deepEqual(await listExecutions(controller.signal), executions);
});

test('lists output runs and builds nested file download URLs', async () => {
  const controller = new AbortController();
  const runs = [{ execution_id: 'attempt-id', project_name: 'Demo', files: [{ path: 'models/final model.pt' }] }];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/outputs');
    assert.equal(options.signal, controller.signal);
    return Response.json(runs);
  };
  assert.deepEqual(await listOutputRuns(controller.signal), runs);
  assert.equal(outputDownloadUrl('attempt-id', 'models/final model.pt'), '/api/projects/executions/attempt-id/output?path=models%2Ffinal+model.pt');
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

test('uploads dataset as multipart and links it to a job', async () => {
  const { uploadDataset, getTrainingJob } = await import('./api.js');
  const file = new Blob(['a,b\n1,2'], { type: 'text/csv' });
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/project-id/datasets');
    assert.equal(options.method, 'POST');
    assert.equal(await options.body.get('file').text(), 'a,b\n1,2');
    assert.equal(options.headers, undefined);
    return Response.json({ id: 'dataset-id' }, { status: 201 });
  };
  const dataset = await uploadDataset('project-id', file);
  globalThis.fetch = async (url, options) => {
    assert.deepEqual(JSON.parse(options.body), { entrypoint: 'train.py', epochs: 2, dataset_id: dataset.id });
    return Response.json({ id: 'job-id' });
  };
  await submitTraining('project-id', 'train.py', 2, dataset.id);
  const controller = new AbortController();
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/project-id/training-jobs/job-id');
    assert.equal(options.signal, controller.signal);
    return Response.json({ status: 'finished', dataset: { deleted_at: '2026-09-24T00:00:00Z' } });
  };
  assert.equal((await getTrainingJob('project-id', 'job-id', controller.signal)).status, 'finished');
});

test('checks startup with mapped data and no arguments, then submits the checked job', async () => {
  const { confirmTraining } = await import('./api.js');
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/project-id/training-jobs');
    assert.deepEqual(JSON.parse(options.body), {
      entrypoint: 'train.py', epochs: null, dataset_id: 'dataset-id', dataset_target: 'data/train.csv',
    });
    return Response.json({ id: 'check-id', startup_check: true });
  };
  await submitTraining('project-id', 'train.py', null, 'dataset-id', 'data/train.csv');
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/projects/project-id/training-jobs/check-id/submit');
    assert.equal(options.method, 'POST');
    return Response.json({ id: 'check-id', startup_check: false, status: 'queued' });
  };
  assert.equal((await confirmTraining('project-id', 'check-id')).startup_check, false);
});
