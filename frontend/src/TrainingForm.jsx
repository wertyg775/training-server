import React, { useEffect, useRef, useState } from 'react';
import { confirmTraining, getTrainingJob, submitTraining } from './api.js';

export default function TrainingForm({ projectId, path, dataset, onBusyChange }) {
  const [epochs, setEpochs] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [job, setJob] = useState(null);
  const target = dataset?.target || '';
  const inFlight = useRef(false);
  const runnable = path?.endsWith('.py');
  const active = job && ['queued', 'building', 'running'].includes(job.status);
  const busy = Boolean(pending || active);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  const configuration = JSON.stringify([projectId, path, epochs, dataset?.id, target]);
  const checkedConfiguration = useRef('');
  const checked = job?.startup_check && job.status === 'checked' && checkedConfiguration.current === configuration;

  useEffect(() => {
    if (!active) return undefined;
    const controller = new AbortController();
    let timer;
    async function refresh() {
      try {
        const updated = await getTrainingJob(projectId, job.id, controller.signal);
        if (controller.signal.aborted) return;
        setJob(updated);
      } catch (failure) {
        if (!controller.signal.aborted) setError(`Could not refresh status: ${failure.message}`);
      }
      if (!controller.signal.aborted) timer = setTimeout(refresh, 2000);
    }
    timer = setTimeout(refresh, 1000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [projectId, job?.id, active]);

  async function action(operation) {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setError('');
    try { await operation(); }
    catch (failure) { setError(failure.message); }
    finally { inFlight.current = false; setPending(false); }
  }

  async function check(event) {
    event.preventDefault();
    const count = epochs === '' ? null : Number(epochs);
    if (count !== null && (!Number.isInteger(count) || count < 1 || count > 2147483647)) {
      setError('Enter a positive whole number of epochs, or leave it blank.');
      return;
    }
    await action(async () => {
      const result = await submitTraining(projectId, path, count, dataset?.id, target);
      checkedConfiguration.current = configuration;
      setJob(result);
    });
  }

  return <>
    <div className="file-preview-header">
      <h3 title={path}>{path || 'File contents'}</h3>
      <form id="training-request" className="training-form" onSubmit={check} aria-label="Check training startup">
        <label htmlFor="training-epochs">Epochs (optional)</label>
        <input id="training-epochs" type="number" min="1" max="2147483647" step="1" value={epochs} disabled={!runnable || busy} onChange={(event) => setEpochs(event.target.value)} />
        <button type="submit" disabled={!runnable || busy || Boolean(dataset && !target.trim()) || Boolean(dataset && job?.dataset?.id === dataset.id)}>{pending ? 'Saving…' : 'Check startup'}</button>
        <button type="button" disabled={!checked || busy} onClick={() => action(async () => setJob(await confirmTraining(projectId, job.id)))}>Submit training</button>
      </form>
    </div>
    {job && <div className="tree-message" aria-live="polite">
      <p>{job.startup_check ? 'Startup check' : 'Training'}: <strong>{job.status}</strong> · {job.entrypoint}</p>
      {job.environment_validation?.message && <p>{job.environment_validation.message}</p>}
      {job.environment_validation?.output && <pre>{job.environment_validation.output}</pre>}
      {job.error && <p role="alert">{job.error}</p>}
      {job.dataset?.expires_at && <p>Dataset expires {new Date(job.dataset.expires_at).toLocaleString()}.</p>}
      <details>
        <summary>Advanced</summary>
        <p><a href={`/api/projects/${projectId}/training-jobs/${job.id}/dockerfile`} download="Dockerfile">Download Dockerfile</a></p>
      </details>
    </div>}
    {error && <p className="tree-message" role="alert">{error}</p>}
  </>;
}
