import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import React from 'react';

let downloads: any[] = [];
const invalidate = mock(() => {});
const trigger = mock(async (_name: string) => {});
const toast = mock(() => {});
mock.module('@tanstack/react-query', () => ({
  useQuery: () => ({ data: { downloads } }),
  useQueryClient: () => ({ invalidateQueries: invalidate }),
  useMutation: (options: any) => ({ isPending: false, mutate: (vars: any) => { void trigger(vars.modelName).then(() => options.onSuccess(null, vars), (err) => options.onError(err)); } }),
}));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, vars?: any) => vars?.pct != null ? `${key}:${vars.pct}` : key }) }));
mock.module('../src/components/ui/button', () => ({ Button: (props: any) => <button {...props} /> }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('../src/lib/api/client', () => ({ apiClient: { triggerModelDownload: trigger } }));
const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { userAgent: 'Macintosh' } });
const { DictationReadinessChecklist } = await import('../src/components/CapturesTab/DictationReadinessChecklist');
let view: ReactTestRenderer;
const model = (name: string, ready = false) => ({ model_name: name, display_name: name, ready, size_mb: 1200 });
let readiness: any;
const render = async (compact = false) => { await act(async () => { view = create(<DictationReadinessChecklist readiness={readiness} compact={compact} />); }); };
const text = () => JSON.stringify(view.toJSON());
const buttons = () => view.root.findAllByType('button');
beforeEach(() => { downloads = []; invalidate.mockClear(); trigger.mockClear(); toast.mockClear(); readiness = { stt: model('speech'), llm: model('refine'), autoRefine: true, inputMonitoring: false, accessibility: false, openInputMonitoringSettings: mock(() => {}), openAccessibilitySettings: mock(() => {}) }; });
afterEach(() => { if (view) act(() => view.unmount()); });
afterAll(() => { mock.restore(); if (previousNavigator) Object.defineProperty(globalThis, 'navigator', previousNavigator); else Reflect.deleteProperty(globalThis, 'navigator'); });

test('shows missing model downloads and macOS permission actions', async () => { await render(); expect(text()).toContain('captures.readiness.stt.missingWithSize'); expect(buttons()).toHaveLength(4); await act(async () => { buttons()[2].props.onClick(); buttons()[3].props.onClick(); }); expect(readiness.openInputMonitoringSettings).toHaveBeenCalledTimes(1); expect(readiness.openAccessibilitySettings).toHaveBeenCalledTimes(1); });
test('ready models hide download actions and optional refinement is omitted', async () => { readiness.stt = model('speech', true); readiness.autoRefine = false; await render(true); expect(text()).not.toContain('captures.readiness.title'); expect(text()).not.toContain('captures.readiness.llm.label'); expect(buttons()).toHaveLength(2); });
test('starting a model download refreshes related data and reports success', async () => { await render(); await act(async () => { buttons()[0].props.onClick(); await Promise.resolve(); }); expect(trigger).toHaveBeenCalledWith('speech'); expect(invalidate.mock.calls.map(([x]: any) => x.queryKey[0])).toEqual(['activeTasks', 'modelStatus', 'capture-readiness']); expect(toast.mock.calls[0][0].title).toBe('captures.readiness.downloadStarted'); });
test('failed download reports the error', async () => { trigger.mockImplementationOnce(async () => { throw new Error('offline'); }); await render(); await act(async () => { buttons()[0].props.onClick(); await Promise.resolve(); }); expect(toast.mock.calls[0][0]).toMatchObject({ variant: 'destructive', description: 'offline' }); });
test.each([{ progress: 42.4, expected: 42 }, { progress: 120, expected: 100 }, { progress: -5, expected: 0 }, { current: 0, total: 100, expected: 0 }, { current: 200, total: 100, expected: 100 }])('download progress is bounded and displays zero: %j', async ({ expected, ...progress }) => { downloads = [{ model_name: 'speech', status: 'downloading', ...progress }]; await render(); expect(buttons()[0].props.disabled).toBe(true); expect(text()).toContain(`captures.readiness.downloadingPercent:${expected}`); });
test('extraction remains an active task and cannot start a duplicate download', async () => { downloads = [{ model_name: 'speech', status: 'extracting', progress: 100 }]; await render(); expect(buttons()[0].props.disabled).toBe(true); expect(text()).toContain('captures.readiness.downloadingPercent:100'); });
test('a completed task refreshes readiness and model status', async () => { downloads = [{ model_name: 'speech', status: 'downloading', progress: 20 }]; await render(); invalidate.mockClear(); downloads = []; await act(async () => view.update(<DictationReadinessChecklist readiness={readiness} />)); expect(invalidate.mock.calls.map(([x]: any) => x.queryKey[0])).toEqual(['capture-readiness', 'modelStatus']); });
