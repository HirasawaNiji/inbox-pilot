"use strict";

(() => {
  const provider = document.querySelector("#llm-provider");
  const model = document.querySelector("#llm-model");
  if (!(provider instanceof HTMLSelectElement) || !(model instanceof HTMLSelectElement)) {
    return;
  }

  const options = Array.from(model.options).map((option) => ({
    provider: option.dataset.provider || "",
    value: option.value,
    label: option.textContent || option.value,
  }));
  const initiallySelected = model.dataset.selectedModel || "";

  const updateModels = (preferred = "") => {
    const matching = options.filter((option) => option.provider === provider.value);
    model.replaceChildren();
    for (const item of matching) {
      const option = new Option(item.label, item.value);
      model.add(option);
    }
    if (matching.some((option) => option.value === preferred)) {
      model.value = preferred;
    }
  };

  updateModels(initiallySelected);
  provider.addEventListener("change", () => updateModels());
})();
