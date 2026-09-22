import React, { useRef, useState } from 'react';
import { submitTraining } from './api.js';

export default function TrainingForm({ projectId, path }) {
  const [epochs, setEpochs] = useState('1');
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState('');
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  const runnable = path?.endsWith('.py');

  async function submit(event) {
    event.preventDefault();
    if (inFlight.current || !runnable) return;
    const count = Number(epochs);
    if (!Number.isInteger(count) || count < 1 || count > 2147483647) {
      setError('Enter a positive whole number of epochs.');
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError('');
    setResult('');
    try {
      const job = await submitTraining(projectId, path, count);
      setResult(`Training request saved (${job.id}).`);
    } catch (failure) {
      setError(failure.message);
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  return <>
    <div className="file-preview-header">
      <h3 title={path}>{path || 'File contents'}</h3>
      <form className="training-form" onSubmit={submit} aria-label="Submit training request">
        <label htmlFor="training-epochs">Epochs</label>
        <input id="training-epochs" type="number" min="1" max="2147483647" step="1" required value={epochs} disabled={!runnable || pending} onChange={(event) => setEpochs(event.target.value)} />
        <button type="submit" disabled={!runnable || pending} title={runnable ? 'Save training request' : 'Select a Python (.py) file to train'}>{pending ? 'Submitting…' : 'Submit'}</button>
      </form>
    </div>
    {result && <p className="tree-message" role="status">{result}</p>}
    {error && <p className="tree-message" role="alert">{error}</p>}
  </>;
}
