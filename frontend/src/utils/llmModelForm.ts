export type TokenLimitParam = 'auto' | 'max_tokens' | 'max_completion_tokens';

interface ModelFormSource {
    provider?: string;
    model?: string;
    label?: string;
    base_url?: string | null;
    api_key_masked?: string;
    supports_vision?: boolean;
    max_output_tokens?: number | null;
    request_timeout?: number | null;
    temperature?: number | null;
    token_limit_param?: TokenLimitParam;
}

export interface LLMModelForm {
    provider: string;
    model: string;
    label: string;
    base_url: string;
    api_key: string;
    supports_vision: boolean;
    max_output_tokens: string;
    request_timeout: string;
    temperature: string;
    token_limit_param: TokenLimitParam;
}

export function createLLMModelForm(model: ModelFormSource = {}): LLMModelForm {
    return {
        provider: model.provider || 'anthropic',
        model: model.model || '',
        label: model.label || '',
        base_url: model.base_url || '',
        api_key: model.api_key_masked || '',
        supports_vision: model.supports_vision || false,
        max_output_tokens: model.max_output_tokens ? String(model.max_output_tokens) : '',
        request_timeout: model.request_timeout ? String(model.request_timeout) : '',
        temperature: model.temperature !== null && model.temperature !== undefined ? String(model.temperature) : '',
        token_limit_param: model.token_limit_param || 'auto',
    };
}

export function buildLLMModelPayload(form: LLMModelForm) {
    return {
        ...form,
        max_output_tokens: form.max_output_tokens ? Number(form.max_output_tokens) : null,
        request_timeout: form.request_timeout ? Number(form.request_timeout) : null,
        temperature: form.temperature !== '' ? Number(form.temperature) : null,
    };
}

export function buildLLMTestPayload(form: LLMModelForm, modelId?: string | null) {
    return {
        provider: form.provider,
        model: form.model,
        base_url: form.base_url || undefined,
        token_limit_param: form.token_limit_param,
        api_key: form.api_key && !form.api_key.startsWith('****') ? form.api_key : undefined,
        model_id: modelId || undefined,
    };
}
