import { useCallback } from 'react'
import { useTranslations } from 'next-intl'

/**
 * Translated display names for catalogue items the server describes in English
 * (promo templates, promo background ambiances, AI models whose name carries a
 * description). The server keeps sending a stable `id` and an English `label`; the UI
 * shows the translation for the ids it knows and the server label for anything else,
 * so a template or model added on the server never shows up as a raw key.
 */
const PROMO_TEMPLATES = ['fresh', 'premium'] as const
const PROMO_AMBIANCES = ['fresh', 'gourmand', 'premium', 'festif', 'frais_bleu', 'neutre'] as const
const GEN_MODELS: Record<string, string> = {
  'fal-ai/flux/dev/image-to-image': 'flux_dev_i2i',
  'fal-ai/nano-banana/edit': 'nano_banana_edit',
  'fal-ai/birefnet': 'birefnet',
  'fal-ai/ltx-video/image-to-video': 'ltx_i2v',
  'fal-ai/wan-25-preview/image-to-video': 'wan25_i2v',
  'fal-ai/kling-video/v2/master/image-to-video': 'kling_v2_i2v',
  'elevenlabs/tts': 'elevenlabs_voice',
  'elevenlabs/music': 'elevenlabs_music',
}

type Item = { id: string; label: string }

export function useServerLabels() {
  const tp = useTranslations('promo')
  const tg = useTranslations('generation')
  const promoTemplate = useCallback(
    (x: Item) => ((PROMO_TEMPLATES as readonly string[]).includes(x.id) ? tp(`tpl_label_${x.id}`) : x.label),
    [tp],
  )
  const promoAmbiance = useCallback(
    (x: Item) => ((PROMO_AMBIANCES as readonly string[]).includes(x.id) ? tp(`amb_label_${x.id}`) : x.label),
    [tp],
  )
  const genModel = useCallback(
    (x: Item) => (GEN_MODELS[x.id] ? tg(`model_label_${GEN_MODELS[x.id]}`) : x.label),
    [tg],
  )
  return { promoTemplate, promoAmbiance, genModel }
}
