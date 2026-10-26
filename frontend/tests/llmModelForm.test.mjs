import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import test from 'node:test';

async function loadFormHelpers() {
    const url = new URL('../src/utils/llmModelForm.ts', import.meta.url);
    assert.ok(existsSync(url), 'Model forms need shared, testable configuration/payload handling');
    return import(url.href);
}

test('legacy model edit defaults to auto and preserves existing numeric configuration', async () => {
    const { createLLMModelForm, buildLLMModelPayload } = await loadFormHelpers();
    const form = createLLMModelForm({ provider: 'custom', model: 'alias', label: 'Old', max_output_tokens: 4096, temperature: 0 });
    const payload = buildLLMModelPayload(form);
    assert.equal(payload.token_limit_param, 'auto');
    assert.equal(payload.max_output_tokens, 4096);
    assert.equal(payload.temperature, 0);
    assert.equal(payload.request_timeout, null);
});

for (const strategy of ['auto', 'max_tokens', 'max_completion_tokens']) {
    test(`save and test use the same unsaved strategy: ${strategy}`, async () => {
        const { createLLMModelForm, buildLLMModelPayload, buildLLMTestPayload } = await loadFormHelpers();
        const form = { ...createLLMModelForm({ provider: 'openai', model: 'gpt-6-sol', token_limit_param: 'max_tokens' }),
            token_limit_param: strategy, api_key: 'new-key', request_timeout: '120' };
        assert.equal(buildLLMModelPayload(form).token_limit_param, strategy);
        assert.equal(buildLLMModelPayload(form).request_timeout, 120);
        const added = buildLLMTestPayload(form);
        const edited = buildLLMTestPayload(form, 'saved-id');
        assert.equal(added.token_limit_param, strategy);
        assert.equal(edited.token_limit_param, strategy);
        assert.equal(added.api_key, 'new-key');
        assert.equal(edited.model_id, 'saved-id');
        assert.equal(added.model_id, undefined);
    });
}

test('stored strategy survives editing; masked key is not sent as a credential', async () => {
    const { createLLMModelForm, buildLLMTestPayload } = await loadFormHelpers();
    const form = createLLMModelForm({ provider: 'custom', model: 'alias', token_limit_param: 'max_completion_tokens', api_key_masked: '****1234' });
    assert.equal(form.token_limit_param, 'max_completion_tokens');
    const payload = buildLLMTestPayload(form, 'saved-id');
    assert.equal(payload.token_limit_param, 'max_completion_tokens');
    assert.equal(payload.api_key, undefined);
    assert.equal(payload.model_id, 'saved-id');
});

test('new form reset does not inherit an edited model strategy', async () => {
    const { createLLMModelForm, buildLLMTestPayload } = await loadFormHelpers();
    const form = createLLMModelForm();
    assert.equal(form.token_limit_param, 'auto');
    assert.equal(form.api_key, '');
    assert.equal(buildLLMTestPayload(form).token_limit_param, 'auto');
});
