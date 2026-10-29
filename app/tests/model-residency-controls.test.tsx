import { afterEach, expect, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { ModelResidencyControls } from '../src/components/ServerSettings/ModelResidencyControls';
import { apiClient } from '../src/lib/api/client';
import type { ModelResidencyStatus, ModelStatus } from '../src/lib/api/types';

const originals = {
  get: apiClient.getModelResidency,
  update: apiClient.updateModelResidency,
  warm: apiClient.prewarmModel,
};
let view: ReactTestRenderer | undefined;
let client: QueryClient;
let state: ModelResidencyStatus;
let calls: string[];
const models: ModelStatus[] = [
  {
    model_name: 'moss-tts',
    display_name: 'MOSS CPU',
    downloaded: true,
    downloading: false,
    loaded: false,
  },
  {
    model_name: 'qwen-tts-1.7B',
    display_name: 'Qwen missing',
    downloaded: false,
    downloading: false,
    loaded: false,
  },
  {
    model_name: 'whisper-base',
    display_name: 'Whisper',
    downloaded: true,
    downloading: false,
    loaded: false,
  },
];
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}
async function mount(list = models) {
  calls = [];
  state = {
    policy: { max_resident_engines: 1, estimated_weight_budget_mb: 16384 },
    persistence: 'session',
    budget_basis: 'Estimated weights',
    selected_model: null,
    resident_models: [],
    last_voice_prewarm: null,
  };
  apiClient.getModelResidency = async () => state;
  apiClient.updateModelResidency = async (policy) => {
    calls.push('policy');
    state = { ...state, policy };
    return state;
  };
  apiClient.prewarmModel = async (modelName, profileId) => {
    calls.push(`warm:${modelName}:${profileId ?? 'weights'}`);
    state = {
      ...state,
      resident_models: [{ model_name: modelName, engine: 'moss_tts', estimated_weight_mb: 763 }],
    };
    return { ...state, mode: 'weights_only', audio_saved: false };
  };
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <ModelResidencyControls models={list} />
      </QueryClientProvider>,
    );
  });
  await flush();
}
afterEach(async () => {
  await act(async () => {
    view?.unmount();
  });
  client?.clear();
  apiClient.getModelResidency = originals.get;
  apiClient.updateModelResidency = originals.update;
  apiClient.prewarmModel = originals.warm;
});
function button(text: string) {
  return view!.root.findAllByType('button').find((node) => node.children.includes(text))!;
}

test('only downloaded speech models can warm; result is explicitly weights-only', async () => {
  await mount();
  expect(view!.root.findAllByType('option').map((node) => node.props.value)).toEqual(['moss-tts']);
  await act(async () => {
    button('Warm model weights').props.onClick();
  });
  await flush();
  expect(calls).toEqual(['warm:moss-tts:weights']);
  expect(JSON.stringify(view!.toJSON())).toContain('Model weights are warm. Voice preparation');
  expect(JSON.stringify(view!.toJSON())).toContain('moss-tts');
});

test('session limits validate integers and apply via the policy API', async () => {
  await mount();
  const engineInput = view!.root.findByProps({ id: 'resident-engine-limit' });
  await act(async () => {
    engineInput.props.onChange({ target: { value: '0' } });
  });
  expect(button('Apply session limits').props.disabled).toBe(true);
  expect(calls).toEqual([]);
  await act(async () => {
    engineInput.props.onChange({ target: { value: '2' } });
  });
  await act(async () => {
    view!.root.findByType('form').props.onSubmit({ preventDefault() {} });
  });
  await flush();
  expect(calls).toEqual(['policy']);
  expect(state.policy.max_resident_engines).toBe(2);
  expect(JSON.stringify(view!.toJSON())).toContain('not a RAM limit');
});

test('unavailable models disable warm and backend refusal stays visible', async () => {
  await mount([]);
  expect(button('Warm model weights').props.disabled).toBe(true);
  await act(async () => {
    view!.unmount();
  });
  client.clear();
  await mount();
  apiClient.prewarmModel = async () => {
    throw new Error('Selected model exceeds the estimated weight budget');
  };
  await act(async () => {
    button('Warm model weights').props.onClick();
  });
  await flush();
  expect(JSON.stringify(view!.toJSON())).toContain('exceeds the estimated weight budget');
});

test('policy fields stay disabled until the submitted policy finishes', async () => {
  await mount();
  let finish!: () => void;
  apiClient.updateModelResidency = (policy) =>
    new Promise((resolve) => {
      finish = () => {
        state = { ...state, policy };
        resolve(state);
      };
    });
  await act(async () => {
    view!.root.findByType('form').props.onSubmit({ preventDefault() {} });
  });
  await flush();
  expect(view!.root.findByProps({ id: 'resident-engine-limit' }).props.disabled).toBe(true);
  expect(view!.root.findByProps({ id: 'resident-weight-budget' }).props.disabled).toBe(true);
  await act(async () => {
    finish();
  });
  await flush();
  expect(view!.root.findByProps({ id: 'resident-engine-limit' }).props.disabled).toBe(false);
});
