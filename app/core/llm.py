"""LLM integration. Responsible for phrasing and translation only.

The assistant is strictly forbidden from deciding facts. All math and routing
decisions are made in context_engine.py. This file receives a ResolvedContext
and turns it into natural language in the fan's chosen language.

Supports: English · Hindi · Spanish · French · Portuguese.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

from app.core.schemas import AccessibilityNeed, AssistantReply, Language, ResolvedContext

logger = logging.getLogger("phoenix.llm")

# ---------------------------------------------------------------------------
# Response templates — 10 intent keys × 5 languages
# ---------------------------------------------------------------------------
_TEMPLATES: dict[str, dict[Language, str]] = {
    "find_gate": {
        Language.EN: ("Head to {gate}. Current predicted wait is about {wait} minutes ({level})."),
        Language.HI: ("{gate} की ओर जाएं। अनुमानित प्रतीक्षा समय लगभग {wait} मिनट है ({level})."),
        Language.ES: (
            "Dirígete a {gate}. El tiempo de espera estimado es de unos {wait} minutos ({level})."
        ),
        Language.FR: (
            "Dirigez-vous vers {gate}. Le temps d'attente "
            "estimé es d'environ {wait} minutes ({level})."
        ),
        Language.PT: (
            "Vá até {gate}. O tempo de espera estimado é de cerca de {wait} minutos ({level})."
        ),
    },
    "wait_time": {
        Language.EN: (
            "{gate} currently has a predicted wait of {wait} minutes — congestion is {level}."
        ),
        Language.HI: ("{gate} पर वर्तमान में अनुमानित प्रतीक्षा {wait} मिनट है — भीड़ स्तर {level} है।"),
        Language.ES: (
            "{gate} tiene actualmente una espera estimada de {wait} minutos — congestión {level}."
        ),
        Language.FR: (
            "{gate} a actuellement une attente estimée de "
            "{wait} minutes — la congestion est {level}."
        ),
        Language.PT: (
            "{gate} tem atualmente uma espera estimada de "
            "{wait} minutos — o congestionamento está {level}."
        ),
    },
    # Standard wheelchair / step-free accessibility
    "accessibility": {
        Language.EN: ("The step-free route is via {gate}, predicted wait {wait} minutes."),
        Language.HI: ("स्टेप-फ्री रास्ता {gate} से है, अनुमानित प्रतीक्षा {wait} मिनट।"),
        Language.ES: ("La ruta sin escalones es por {gate}, espera estimada de {wait} minutos."),
        Language.FR: (
            "L'itinéraire sans marche se fait via {gate}, attente estimée de {wait} minutes."
        ),
        Language.PT: ("A rota sem degraus é via {gate}, espera estimada de {wait} minutos."),
    },
    # Audio-guided route for visually-impaired fans
    "accessibility_visual": {
        Language.EN: (
            "Your audio-guided step-free route is via {gate}, "
            "predicted wait {wait} minutes. Tactile paving runs "
            "from stiles to elevators, and audio announcements "
            "guide at each junction."
        ),
        Language.HI: (
            "आपका ऑडियो-गाइडेड स्टेप-फ्री मार्ग {gate} से है, "
            "अनुमानित प्रतीक्षा {wait} मिनट। टिकट खिड़की से "
            "लिफ्ट तक स्पर्शनीय मार्ग उपलब्ध है, और ऑडियो "
            "निर्देश हर मोड़ पर सहायता करेंगे।"
        ),
        Language.ES: (
            "Tu ruta guiada por audio sin escalones es por {gate}, "
            "espera estimada de {wait} minutos. Hay pavimentos "
            "táctiles hacia los ascensores y anuncios de audio "
            "en cada cruce."
        ),
        Language.FR: (
            "Votre itinéraire audio-guidée sans marches passe par "
            "{gate}, attente estimée {wait} minutes. Des bandes "
            "d'aide à l'orientation mènent aux ascenseurs, et "
            "des annonces sonores guident chaque croisement."
        ),
        Language.PT: (
            "Sua rota sem degraus com guia de áudio é via {gate}, "
            "espera estimada de {wait} minutos. Piso tátil "
            "orienta o caminho até os elevadores, e avisos "
            "sonoros auxiliam em cada cruzamento."
        ),
    },
    # Visual-display route for hearing-impaired fans
    "accessibility_hearing": {
        Language.EN: (
            "Your visual-display route is via {gate}, predicted "
            "wait {wait} minutes. High-brightness LED wayfinding "
            "boards at every concourse and junction will guide "
            "you through."
        ),
        Language.HI: (
            "आपका विजुअल-डिस्प्ले मार्ग {gate} से है, अनुमानित "
            "प्रतीक्षा {wait} मिनट। हर कॉरिडोर और मोड़ पर "
            "चमकीले LED बोर्ड स्पष्ट रूप से आपका मार्गदर्शन करेंगे।"
        ),
        Language.ES: (
            "Tu ruta con pantallas visuales es por {gate}, espera "
            "estimada de {wait} minutos. Paneles de señalización "
            "LED de alta luminosidade en cada pasillo y cruce "
            "te guiarán."
        ),
        Language.FR: (
            "Votre itinéraire avec panneaux visuels es via {gate}, "
            "attente estimée {wait} minutes. Des écrans LED "
            "haute luminosité à chaque hall et intersection "
            "guideront votre progression."
        ),
        Language.PT: (
            "Sua rota com painéis visuais é via {gate}, espera "
            "estimada de {wait} minutos. Painéis LED de alto "
            "brilho para orientação em cada corredor e cruzamento "
            "guiarão você."
        ),
    },
    "crowd_status": {
        Language.EN: (
            "The least congested entry right now is {gate} at {level} congestion ({wait} min wait)."
        ),
        Language.HI: (
            "अभी सबसे कम भीड़ वाला प्रवेश द्वार {gate} है, भीड़ स्तर {level} ({wait} मिनट प्रतीक्षा)।"
        ),
        Language.ES: (
            "La entrada menos congestionada ahora es {gate}, congestión {level} ({wait} min)."
        ),
        Language.FR: (
            "L'entrée la moins encombrée en ce moment est {gate} "
            "avec une congestion {level} ({wait} min d'attente)."
        ),
        Language.PT: (
            "A entrada menos congestionada agora é {gate} com "
            "congestionamento {level} ({wait} min de espera)."
        ),
    },
    "transport": {
        Language.EN: (
            "Shuttle drop-off and public transport connections are "
            "closest to {gate}. Parking updates are posted at all gates."
        ),
        Language.HI: (
            "शटल ड्रॉप-ऑफ और सार्वजनिक परिवहन कनेक्शन {gate} के "
            "सबसे निकट हैं। पार्किंग अपडेट सभी गेटों पर उपलब्ध हैं।"
        ),
        Language.ES: (
            "La parada de transporte y las conexiones de transporte "
            "público están más cerca de {gate}. Hay actualizaciones "
            "de parking en todas las puertas."
        ),
        Language.FR: (
            "Le dépôt de la navette et les connexions de transport "
            "public sont les plus proches de {gate}. Les mises à "
            "jour du parking sont affichées à todas les portes."
        ),
        Language.PT: (
            "O desembarque do traslado e as conexões de transporte "
            "público ficam mais próximos do {gate}. Atualizações "
            "de estacionamento estão em todos os portões."
        ),
    },
    "restroom": {
        Language.EN: "Restrooms are available near every gate concourse, including near {gate}.",
        Language.HI: "शौचालय हर गेट कॉरिडोर के पास उपलब्ध हैं, जिसमें {gate} के पास भी शामिल है।",
        Language.ES: "Hay baños disponibles cerca de cada puerta, incluyendo cerca de {gate}.",
        Language.FR: (
            "Des toilettes sont disponibles près de chaque hall de porte, y compris près de {gate}."
        ),
        Language.PT: (
            "Banheiros estão disponíveis perto de cada saguão de portão, inclusive perto do {gate}."
        ),
    },
    "sustainability": {
        Language.EN: (
            "Recycling points are at every concourse exit — the "
            "closest to {gate} is right by the exit. Thank you for "
            "keeping the stadium green!"
        ),
        Language.HI: (
            "पुनर्चक्रण बिंदु हर गेट कॉरिडोर के निकास पर हैं — "
            "{gate} के सबसे पास निकास के ठीक बगल में है। "
            "स्टेडियम को हरा-भरा रखने के लिए धन्यवाद!"
        ),
        Language.ES: (
            "Hay puntos de reciclaje en cada salida — el más "
            "cercano a {gate} está justo al lado de la salida. "
            "¡Gracias por mantener el estadio sostenible!"
        ),
        Language.FR: (
            "Des points de recyclage se trouvent à chaque sortie — "
            "le plus proche de {gate} est juste à côté de la sortie. "
            "Merci de garder le stade vert !"
        ),
        Language.PT: (
            "Os pontos de reciclagem estão em cada saída — o "
            "mais próximo do {gate} fica logo na saída. "
            "Obrigado por manter o estádio sustentável!"
        ),
    },
    "emergency": {
        Language.EN: "{safety}",
        Language.HI: "{safety}",
        Language.ES: "{safety}",
        Language.FR: "{safety}",
        Language.PT: "{safety}",
    },
    "general_info": {
        Language.EN: "The best entry point right now is {gate}, predicted wait {wait} minutes.",
        Language.HI: "अभी सबसे अच्छा प्रवेश बिंदु {gate} है, अनुमानित प्रतीक्षा {wait} मिनट।",
        Language.ES: (
            "El mejor punto de entrada ahora es {gate}, espera estimada de {wait} minutos."
        ),
        Language.FR: (
            "Le meilleur point d'entrée en ce moment es {gate}, attente estimée de {wait} minutes."
        ),
        Language.PT: "O melhor ponto de entrada agora é {gate}, espera estimada de {wait} minutos.",
    },
    "lost_and_found": {
        Language.EN: (
            "Report a lost item, or check what's been handed in, "
            "at the guest services desk near the main concourse."
        ),
        Language.HI: (
            "खोई हुई वस्तु की सूचना दें, या मुख्य मार्ग के पास अतिथि सेवा डेस्क पर जमा वस्तुओं की जांच करें।"
        ),
        Language.ES: (
            "Reporta un objeto perdido, o consulta lo entregado, "
            "en el mostrador de atención al público cerca del "
            "paso principal."
        ),
        Language.FR: (
            "Signalez un objet perdu, ou vérifiez ce qui a été "
            "déposé, au bureau des services aux visiteurs près "
            "du hall principal."
        ),
        Language.PT: (
            "Relate um item perdido, ou verifique o que foi "
            "entregue, no balcão de atendimento ao visitante "
            "perto do saguão principal."
        ),
    },
}

_CONGESTION_LABELS: dict[Language, dict[str, str]] = {
    Language.EN: {"low": "low", "moderate": "moderate", "high": "high", "critical": "critical"},
    Language.HI: {"low": "कम", "moderate": "मध्यम", "high": "अधिक", "critical": "गंभीर"},
    Language.ES: {"low": "bajo", "moderate": "moderado", "high": "alto", "critical": "crítico"},
    Language.FR: {"low": "faible", "moderate": "modéré", "high": "élevé", "critical": "critique"},
    Language.PT: {"low": "baixo", "moderate": "moderado", "high": "alto", "critical": "crítico"},
}

_ALTERNATE_SUFFIX: dict[Language, str] = {
    Language.EN: " If that's busy, {gate} is the next best option at about {wait} minutes.",
    Language.HI: " अगर वह व्यस्त है, तो {gate} अगला सबसे अच्छा विकल्प है, लगभग {wait} मिनट में।",
    Language.ES: " Si está ocupado, {gate} es la siguiente mejor opción, con unos {wait} minutos.",
    Language.FR: (
        " Si c'est occupé, {gate} est la meilleure alternative, avec environ {wait} minutes."
    ),
    Language.PT: (
        " Si estiver ocupado, {gate} é a próxima melhor opção, com cerca de {wait} minutos."
    ),
}


class BaseLLM(ABC):
    @abstractmethod
    def phrase(self, ctx: ResolvedContext) -> AssistantReply: ...


class MockLLM(BaseLLM):
    """Deterministic, offline, zero-dependency. Default provider — demo never breaks."""

    def phrase(self, ctx: ResolvedContext) -> AssistantReply:
        lang = ctx.language

        # Select sub-template for accessibility intents based on the specific need
        intent_key = ctx.intent
        if ctx.intent == "accessibility":
            if ctx.accessibility_need == AccessibilityNeed.VISUAL:
                intent_key = "accessibility_visual"
            elif ctx.accessibility_need == AccessibilityNeed.HEARING:
                intent_key = "accessibility_hearing"

        template = _TEMPLATES.get(intent_key, _TEMPLATES["general_info"])[lang]
        gate_name = ctx.recommended_gate.name if ctx.recommended_gate else "the main concourse"
        wait_min = ctx.wait_estimate.predicted_wait_minutes if ctx.wait_estimate else 0
        level = ctx.wait_estimate.congestion_level if ctx.wait_estimate else "low"

        localized_level = _CONGESTION_LABELS.get(lang, _CONGESTION_LABELS[Language.EN]).get(
            level, level
        )
        text = template.format(
            gate=gate_name,
            wait=wait_min,
            level=localized_level,
            safety=ctx.safety_notice or "",
        )

        if ctx.alternate_gate and ctx.intent in (
            "find_gate",
            "wait_time",
            "crowd_status",
            "general_info",
        ):
            alt_wait = ctx.alternate_wait.predicted_wait_minutes if ctx.alternate_wait else "?"
            suffix_template = _ALTERNATE_SUFFIX.get(lang, _ALTERNATE_SUFFIX[Language.EN])
            text += suffix_template.format(gate=ctx.alternate_gate.name, wait=alt_wait)

        # Prepend safety notices if they exist and it's not an emergency
        if ctx.safety_notice and ctx.intent != "emergency":
            text = f"[{ctx.safety_notice}] {text}"

        facts = [f"intent={ctx.intent}"]
        if ctx.recommended_gate:
            facts.append(f"gate={ctx.recommended_gate.gate_id}")
        if ctx.alternate_gate:
            facts.append(f"alternate_gate={ctx.alternate_gate.gate_id}")
        if ctx.wait_estimate:
            facts.append(f"wait_minutes={ctx.wait_estimate.predicted_wait_minutes}")
            facts.append(f"congestion={ctx.wait_estimate.congestion_level}")

        return AssistantReply(text=text, intent=ctx.intent, grounded_facts=facts, language=lang)


class GeminiLLM(BaseLLM):
    """Live model for phrasing/translation ONLY. It receives the already
    resolved facts as a locked JSON block and is instructed to translate/
    phrase, never to add, remove, or alter a fact. If it ever disagrees
    with the facts it was given, the facts win — this class does not
    let model output override ctx values, it only forwards phrasing."""

    def __init__(self) -> None:
        # optional dep, imported lazily
        import google.generativeai as genai  # type: ignore[import-not-found]

        api_key = os.environ["GEMINI_API_KEY"]
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel("gemini-2.0-flash")

    def phrase(self, ctx: ResolvedContext) -> AssistantReply:
        base = MockLLM().phrase(ctx)
        lang_name = {
            Language.EN: "English",
            Language.HI: "Hindi",
            Language.ES: "Spanish",
            Language.FR: "French",
            Language.PT: "Portuguese",
        }.get(ctx.language, ctx.language.value)
        prompt = (
            f"Rephrase this venue-assistant message naturally in {lang_name}. "
            "Do not change any numbers, gate names, or facts. "
            f"Message: {base.text}"
        )
        try:
            resp = self._model.generate_content(prompt)
            candidate = resp.text.strip()
            # Double check that it contains the core facts; if the model hallucinates
            # or wipes the facts, fallback to MockLLM.
            if ctx.recommended_gate and ctx.recommended_gate.name not in candidate:
                return base
            if ctx.wait_estimate and str(ctx.wait_estimate.predicted_wait_minutes) not in candidate:
                return base
            return AssistantReply(
                text=candidate,
                intent=base.intent,
                grounded_facts=base.grounded_facts,
                language=ctx.language,
            )
        except Exception as exc:
            logger.warning("Gemini LLM phrasing failed: %s", exc)
        return base


def get_llm() -> BaseLLM:
    provider = os.environ.get("PHOENIX_STADIUM_LLM_PROVIDER", "mock")
    if provider == "gemini" and os.environ.get("GEMINI_API_KEY"):
        try:
            return GeminiLLM()
        except Exception as exc:
            logger.warning("Failed to initialize GeminiLLM: %s", exc)
            return MockLLM()
    return MockLLM()
