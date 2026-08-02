const state = {
  flows: [],
  flow: null,
  participant: {},
  participantKey: null,
  participantLoggedIn: false,
  currentSubmission: null,
  answers: {},
  answerReviews: {},
  videoOrder: [],
  usagePolicy: null,
  usagePollTimer: null,
  pageInstanceId: "",
  pageNavigationType: "navigate",
  pageEventReported: false,
  dirtyAnswerKeys: new Set(),
  flatQuestions: [],
  currentIndex: 0,
  autosaveTimer: null,
  saveInFlight: null,
  adminUnlocked: false,
  results: [],
  selectedResultIndex: -1,
  videoPreviewManifests: new Map(),
  videoText: {
    expanded: false,
    currentPath: "",
    currentBaseKey: "",
    language: "original",
    translationAvailable: false,
    cache: {},
    pendingSelection: null,
    activeReferenceId: "",
  },
  explanationEditorSelection: null,
  resultView: {
    videoIndex: 0,
    dimensionIndex: 0,
    questionIndex: 0,
    mode: "detail",
  },
};

const views = ["participant", "instructions", "evaluation", "admin", "results"];
const ADMIN_PASSWORD_STORAGE_KEY = "human-eval-admin-password";
const TEXT_REFERENCE_MAX_CHARS = 500;
const TEXT_REFERENCE_TOTAL_MAX_CHARS = 2500;
const TEXT_REFERENCE_PREVIEW_CHARS = 12;

document.addEventListener("DOMContentLoaded", async () => {
  bindNavigation();
  bindActions();
  initializePageUsageEvent();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopUsagePolicyPolling();
    else startUsagePolicyPolling();
  });
  try {
    await loadFlows();
    await restoreParticipantSession();
    showView("participant");
  } catch (error) {
    showAlert(error.message || String(error));
  }
});

window.addEventListener("unhandledrejection", (event) => {
  showAlert(event.reason?.message || String(event.reason));
});

window.addEventListener("pagehide", persistProgressOnExit);

function bindNavigation() {
  document.querySelectorAll(".tab[data-view]").forEach((button) => {
    button.addEventListener("click", () => navigateTo(button.dataset.view));
  });
}

function bindActions() {
  document.getElementById("participantNext").addEventListener("click", loginParticipant);
  document.getElementById("logoutParticipant").addEventListener("click", logoutParticipant);
  document.getElementById("instructionsBack").addEventListener("click", () => showView("participant"));
  document.getElementById("startEvaluation").addEventListener("click", () => {
    if (!state.participantLoggedIn) {
      showAlert("Enter participant information and sign in first.");
      showView("participant");
      return;
    }
    renderEvaluation();
    showView("evaluation");
  });
  document.getElementById("prevItem").addEventListener("click", () => moveQuestion(-1));
  document.getElementById("nextItem").addEventListener("click", () => moveQuestion(1));
  document.getElementById("prevVideo").addEventListener("click", () => moveVideo(-1));
  document.getElementById("nextVideo").addEventListener("click", () => moveVideo(1));
  document.getElementById("expandVideoText").addEventListener("click", expandVideoText);
  document.getElementById("collapseVideoText").addEventListener("click", collapseVideoText);
  document.getElementById("toggleVideoTextLanguage").addEventListener("click", toggleVideoTextLanguage);
  document.getElementById("expandedToggleVideoTextLanguage").addEventListener("click", toggleVideoTextLanguage);
  bindVideoTextReferenceActions();
  document.getElementById("prevDimension").addEventListener("click", () => moveDimension(-1));
  document.getElementById("nextDimension").addEventListener("click", () => moveDimension(1));
  document.getElementById("prevQuestion").addEventListener("click", () => moveQuestionWithinDimension(-1));
  document.getElementById("nextQuestion").addEventListener("click", () => moveQuestionWithinDimension(1));
  bindDropdownToggle("videoSelectButton", "videoSelectMenu");
  bindDropdownToggle("dimensionSelectButton", "dimensionSelectMenu");
  bindDropdownToggle("questionSelectButton", "questionSelectMenu");
  document.addEventListener("click", closeDropdownsOnOutsideClick);
  document.getElementById("submitEvaluation").addEventListener("click", submitEvaluation);
  document.getElementById("saveDraft").addEventListener("click", () => saveFlow("draft"));
  document.getElementById("publishFlow").addEventListener("click", publishFlow);
  document.getElementById("reloadFlow").addEventListener("click", loadSelectedFlow);
  document.getElementById("flowSelect").addEventListener("change", loadSelectedFlow);
  document.getElementById("exportCsv").addEventListener("click", () => {
    downloadCsv().catch((error) => showAlert(error.message || String(error)));
  });
  document.getElementById("toggleResultCompletion").addEventListener("click", toggleResultCompletionOverview);
  document.getElementById("closeResultDetail").addEventListener("click", closeResultDetail);
  document.getElementById("resultDetailViewTab").addEventListener("click", () => setResultDetailMode("detail"));
  document.getElementById("resultStatsTab").addEventListener("click", () => setResultDetailMode("stats"));
  document.getElementById("resultVideoSelect").addEventListener("change", (event) => selectResultVideo(Number(event.target.value)));
  document.getElementById("resultDimensionSelect").addEventListener("change", (event) => selectResultDimension(Number(event.target.value)));
  document.getElementById("resultQuestionSelect").addEventListener("change", (event) => selectResultQuestion(Number(event.target.value)));
  document.getElementById("prevResultVideo").addEventListener("click", () => moveResultVideo(-1));
  document.getElementById("nextResultVideo").addEventListener("click", () => moveResultVideo(1));
  document.getElementById("prevResultDimension").addEventListener("click", () => moveResultDimension(-1));
  document.getElementById("nextResultDimension").addEventListener("click", () => moveResultDimension(1));
  document.getElementById("prevResultQuestion").addEventListener("click", () => moveResultQuestion(-1));
  document.getElementById("nextResultQuestion").addEventListener("click", () => moveResultQuestion(1));
  document.getElementById("markAnswerForRevision").addEventListener("click", () => {
    markCurrentResultAnswerForRevision().catch((error) => showAlert(error.message || String(error)));
  });
  document.getElementById("reloadAdminTraffic").addEventListener("click", () => {
    loadAdminTraffic().catch((error) => showAlert(error.message || String(error)));
  });
}

async function loadFlows() {
  const response = await apiGet("/api/flows");
  state.flows = response.flows || [];
  if (!state.flows.length) {
    showAlert("No evaluation workflow is available. Ask an administrator to publish one.");
    return;
  }
  const published = state.flows.find((flow) => flow.status === "published");
  state.flow = published || state.flows[0];
  state.flatQuestions = flattenQuestions(state.flow);
  renderFlowSummary();
  renderParticipantForm();
  renderInstructions();
  updateNavigationState();
}

function initializePageUsageEvent() {
  state.pageInstanceId =
    globalThis.crypto?.randomUUID?.()
    || `page-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const navigationEntry = performance.getEntriesByType?.("navigation")?.[0];
  const navigationType = String(navigationEntry?.type || "navigate");
  state.pageNavigationType = ["navigate", "reload", "back_forward", "prerender"].includes(navigationType)
    ? navigationType
    : "navigate";
}

async function restoreParticipantSession() {
  const response = await fetch("/api/participant-session/current");
  if (response.status === 401 || response.status === 403 || response.status === 404) return false;
  const payload = await readApiResponse(response);
  hydrateParticipantSession(payload);
  await reportPageUsageEvent().catch(() => {});
  return true;
}

function hydrateParticipantSession(response, fallbackParticipant = {}) {
  const submission = response.submission || null;
  const matchingFlow = state.flows.find((flow) => flow.id === submission?.flow_id);
  if (matchingFlow) state.flow = matchingFlow;
  state.participantLoggedIn = true;
  state.participantKey = response.participant_key || submission?.participant_key || null;
  state.currentSubmission = submission;
  state.participant = submission?.participant || fallbackParticipant;
  state.answers = submission?.answers || {};
  state.answerReviews = submission?.answer_reviews || {};
  state.videoOrder = submission?.video_order || [];
  state.dirtyAnswerKeys = new Set();
  state.flatQuestions = flattenQuestions(state.flow, state.videoOrder);
  state.currentIndex = firstIncompleteIndex();
  const restored = restoreDraftIfUseful(submission);
  if (restored) scheduleProgressSave();
  applyUsagePolicy(response.usage);
  saveDraft();
  renderParticipantForm();
  renderFlowSummary();
  renderInstructions();
  updateNavigationState();
}

async function reportPageUsageEvent() {
  if (
    state.pageEventReported
    || !state.participantLoggedIn
    || !state.pageInstanceId
  ) {
    return;
  }
  const response = await apiPost("/api/usage/page-event", {
    page_instance_id: state.pageInstanceId,
    event_type: "page_load",
    navigation_type: state.pageNavigationType,
  });
  state.pageEventReported = true;
  applyUsagePolicy(response.usage);
}

async function navigateTo(name) {
  if (["instructions", "evaluation"].includes(name) && !state.participantLoggedIn) {
    showAlert("Enter participant information and sign in first.");
    showView("participant");
    return;
  }
  if (name === "admin") {
    if (!(await ensureAdminAccess())) return;
    await loadAdminFlows();
    await loadAdminTraffic();
    showView("admin");
    return;
  }
  if (name === "results") {
    if (!(await ensureAdminAccess())) return;
    showView("results");
    await renderResults();
    return;
  }
  showView(name);
}

function renderFlowSummary() {
  const flow = state.flow;
  if (!flow) {
    document.getElementById("flowSummary").textContent = "No evaluation workflow is available";
    return;
  }
  const participant = state.participantLoggedIn ? ` | Participant: ${participantDisplayName()}` : "";
  document.getElementById("flowSummary").textContent = `${flow.title} | ${flowStatusLabel(flow.status)} | Version ${flow.version || 1}${participant}`;
}

function renderParticipantForm() {
  const form = document.getElementById("participantForm");
  form.innerHTML = "";
  document.getElementById("participantStatus").textContent = state.participantLoggedIn
    ? `Signed in as ${participantDisplayName()}. Sign out to switch participants.`
    : "Enter a participant identifier. Signing in again with the same identifier restores prior progress.";
  document.getElementById("participantNext").textContent = state.participantLoggedIn ? "Update and View Instructions" : "Sign In and View Instructions";
  document.getElementById("logoutParticipant").hidden = !state.participantLoggedIn;
  for (const field of state.flow.participantFields || []) {
    const row = document.createElement("div");
    row.className = "form-row";
    const label = document.createElement("label");
    const displayLabel = participantFieldLabel(field);
    label.setAttribute("for", `participant-${field.id}`);
    label.textContent = field.required ? `${displayLabel} *` : displayLabel;
    const input = document.createElement("input");
    input.id = `participant-${field.id}`;
    input.name = field.id;
    input.type = field.type || "text";
    input.placeholder = participantFieldPlaceholder(field);
    input.value = state.participant[field.id] || "";
    row.append(label, input);
    form.append(row);
  }
}

function readParticipantForm() {
  const participant = {};
  const missing = [];
  for (const field of state.flow.participantFields || []) {
    const input = document.getElementById(`participant-${field.id}`);
    participant[field.id] = input ? input.value.trim() : "";
    if (field.required && !participant[field.id]) missing.push(participantFieldLabel(field));
  }
  if (missing.length) {
    showAlert(`Complete the required participant fields: ${missing.join(", ")}`);
    return null;
  }
  state.participant = participant;
  if (state.participantLoggedIn) saveDraft();
  clearAlert();
  return participant;
}

async function loginParticipant() {
  const participant = readParticipantForm();
  if (!participant) return;
  const response = await apiPost("/api/participant-session", {
    flow_id: state.flow.id,
    participant,
  });
  hydrateParticipantSession(response, participant);
  await reportPageUsageEvent().catch(() => {});
  showAlert(
    Object.keys(state.answers).length
      ? "Previous progress was restored and can be edited."
      : "A new evaluation record was created.",
    false,
  );
  showView("instructions");
}

async function logoutParticipant() {
  if (state.participantLoggedIn && !document.getElementById("evaluationView").hidden) {
    syncCurrentAnswer();
    await flushProgressSave();
  }
  await apiPost("/api/participant-session/logout", {}).catch(() => {});
  stopUsagePolicyPolling();
  state.participant = {};
  state.participantKey = null;
  state.participantLoggedIn = false;
  state.currentSubmission = null;
  state.answers = {};
  state.answerReviews = {};
  state.videoOrder = [];
  state.usagePolicy = null;
  state.dirtyAnswerKeys = new Set();
  state.currentIndex = 0;
  renderParticipantForm();
  renderFlowSummary();
  updateNavigationState();
  renderUsagePolicyBanner();
  showAlert("Signed out. Enter another participant identifier to start or resume an evaluation.", false);
  showView("participant");
}

function participantDisplayName() {
  return participantName(state.participant, state.flow);
}

function participantName(participant, flow = state.flow) {
  if (!participant) return "Unknown identifier";
  const preferredIds = ["participant_name", "participant_code", "name", "username", "subject_name", "subject_code"];
  for (const id of preferredIds) {
    const value = String(participant[id] || "").trim();
    if (value) return value;
  }
  for (const field of flow?.participantFields || []) {
    const value = String(participant[field.id] || "").trim();
    if (value) return value;
  }
  const fallback = Object.values(participant).find((value) => String(value || "").trim());
  return fallback ? String(fallback).trim() : "Unknown identifier";
}

function participantFieldLabel(field) {
  if (field.id === "participant_code" || field.id === "participant_name") return "Participant identifier";
  return field.label || field.id;
}

function participantFieldPlaceholder(field) {
  if (field.id === "participant_code" || field.id === "participant_name") return "Enter participant identifier";
  return field.placeholder || "";
}

function renderInstructions() {
  const instructions = state.flow.instructions || {};
  document.getElementById("instructionsTitle").textContent = instructions.title || "Evaluation Instructions";
  document.getElementById("instructionsOverview").textContent = instructions.overview || "";

  const exampleBox = document.getElementById("exampleVideoBox");
  if (instructions.exampleVideoPath && state.participantLoggedIn) {
    exampleBox.innerHTML = `<video controls src="${escapeAttribute(videoSource(instructions.exampleVideoPath))}"></video>`;
    attachVideoPreview(exampleBox.querySelector("video"), instructions.exampleVideoPath, state.flow);
  } else if (instructions.exampleVideoPath) {
    removeVideoPreviewTimeline(exampleBox);
    exampleBox.textContent = "Sign in to load the example video.";
  } else {
    removeVideoPreviewTimeline(exampleBox);
    exampleBox.textContent = "No example video is configured for this workflow.";
  }

  const scoringGuide = document.getElementById("scoringGuide");
  scoringGuide.innerHTML = "";
  for (const item of instructions.scoringGuide || []) {
    const div = document.createElement("div");
    div.className = "guide-item";
    div.textContent = item;
    scoringGuide.append(div);
  }

  const dimensionGuide = document.getElementById("dimensionGuide");
  dimensionGuide.innerHTML = "";
  for (const dimension of state.flow.dimensions || []) {
    const card = document.createElement("article");
    card.className = "dimension-card";
    const questions = dimension.questions || [];
    card.innerHTML = `
      <h3>${escapeHtml(dimension.title)}</h3>
      <p>${escapeHtml(dimension.description || "")}</p>
      <div class="subdimension-count">${questions.length} criteria</div>
      <ul class="subdimension-list">
        ${questions.map((question) => `<li>${escapeHtml(question.prompt)}</li>`).join("")}
      </ul>
    `;
    dimensionGuide.append(card);
  }

  if (state.participantLoggedIn) {
    renderInstructionAnswerExample(instructions.answerExample || {}, instructions);
  } else {
    const example = document.getElementById("evaluationExample");
    example.hidden = true;
    example.innerHTML = "";
  }
}

function renderInstructionAnswerExample(config, instructions = {}) {
  const container = document.getElementById("evaluationExample");
  const dimensions = state.flow.dimensions || [];
  const videos = state.flow.videos || [];
  const scoreConfig = state.flow.responseConfig?.score || { min: 0, max: 5, step: 1 };
  const confidenceConfig = state.flow.responseConfig?.confidence || { min: 1, max: 5, step: 1 };
  const dimensionIndex = Math.max(
    0,
    dimensions.findIndex((dimension) => dimension.id === config.dimensionId),
  );
  const dimension = dimensions[dimensionIndex] || dimensions[0];
  const questions = dimension?.questions || [];
  const questionIndex = Math.max(
    0,
    questions.findIndex((question) => question.id === config.questionId),
  );
  const question = questions[questionIndex] || questions[0];
  if (!dimension || !question) {
    container.hidden = true;
    container.innerHTML = "";
    return;
  }

  const videoTitle = config.videoTitle || "Instruction Example Video";
  const exampleVideoPath = config.videoPath || instructions.exampleVideoPath || "";
  const stillImagePath = config.stillImagePath || "example-still.jpg";
  const score = String(config.score ?? 4);
  const confidence = String(config.confidence ?? 4);
  const explanation =
    config.explanation ||
    "The main subjects, setting, and actions are visible, but the short clip does not show the full context or outcome.";

  container.hidden = false;
  container.innerHTML = `
    <div class="section-title compact">
      <div>
        <h3>Example Response</h3>
        <p class="muted">A read-only example using the same structure as the evaluation workspace.</p>
      </div>
      <span class="badge">Example</span>
    </div>
    <div class="answer-example-screen" aria-label="Read-only evaluation example">
      <div class="answer-example-video">
        <div class="section-title compact selector-title">
          <div class="selector-row selector-title-row" aria-label="Example video selection">
            <button class="mini-nav" type="button" disabled aria-label="Previous example video">&lsaquo;</button>
            <div class="inline-selector heading-selector example-selector">
              <span class="dropdown-label">${escapeHtml(videoTitle)}</span>
              <span class="dropdown-status is-complete">Example</span>
            </div>
            <button class="mini-nav" type="button" disabled aria-label="Next example video">&rsaquo;</button>
          </div>
          <span class="badge">Video 1/1</span>
        </div>
        <div class="video-box example-still-box">
          <img src="${escapeAttribute(videoSource(stillImagePath))}" alt="Still frame from ${escapeAttribute(videoTitle)}">
        </div>
        <p class="muted">The still frame comes from the example video above.</p>
        <div id="instructionExampleVideoTextPanel" class="example-video-text-panel" hidden>
          <div class="example-video-text-title">
            <strong>Transcript</strong>
            <span>Example</span>
          </div>
          <textarea id="instructionExampleVideoText" readonly spellcheck="false"></textarea>
        </div>
      </div>
      <div class="answer-example-question">
        <div class="section-title compact selector-title">
          <div class="selector-title-content">
            <div class="selector-row selector-title-row" aria-label="Example dimension selection">
              <button class="mini-nav" type="button" disabled aria-label="Previous example dimension">&lsaquo;</button>
              <div class="inline-selector heading-selector example-selector">
                <span class="dropdown-label">${escapeHtml(dimension.title)}</span>
                <span class="dropdown-status is-complete">Selected</span>
              </div>
              <button class="mini-nav" type="button" disabled aria-label="Next example dimension">&rsaquo;</button>
            </div>
            <p class="muted">${escapeHtml(dimension.description || "")}</p>
          </div>
          <span class="badge">Dimension ${dimensionIndex + 1}/${dimensions.length}</span>
        </div>
        <div class="selector-row question-title-row" aria-label="Example criterion selection">
          <button class="mini-nav" type="button" disabled aria-label="Previous example criterion">&lsaquo;</button>
          <div class="inline-selector heading-selector example-selector">
            <span class="dropdown-label">${escapeHtml(question.prompt)}</span>
            <span class="dropdown-status is-complete">Selected</span>
          </div>
          <button class="mini-nav" type="button" disabled aria-label="Next example criterion">&rsaquo;</button>
          <span class="badge selector-row-badge">Criterion ${questionIndex + 1}/${questions.length}</span>
        </div>
        <div id="instructionExamplePrompt" class="question-prompt"></div>
        <div class="answer-example-form" aria-label="Example response values">
          ${staticChoiceExampleHtml("Score", scoreConfig, score)}
          ${staticChoiceExampleHtml("Confidence", confidenceConfig, confidence)}
          <div class="example-textarea">
            <span>Evaluation Notes</span>
            <p>${escapeHtml(explanation)}</p>
          </div>
        </div>
      </div>
    </div>
  `;
  renderQuestionPromptInto(document.getElementById("instructionExamplePrompt"), question, state.flow);
  renderInstructionExampleVideoText(exampleVideoPath, config);
}

function renderInstructionExampleVideoText(videoPath, config = {}) {
  const panel = document.getElementById("instructionExampleVideoTextPanel");
  const box = document.getElementById("instructionExampleVideoText");
  if (!panel || !box || !videoPath) return;
  const textPath = videoTextOverridePath({
    fileName: videoPath,
    textFileName: config.textFileName,
    textPath: config.textPath,
    textFile: config.textFile,
  });
  const params = new URLSearchParams({ video_path: mediaRelativePath(videoPath) });
  if (textPath) params.set("text_path", textPath);
  fetch(`/api/video-text?${params.toString()}`)
    .then(async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      return payload;
    })
    .then((payload) => {
      box.value = payload.text || "The transcript file is empty.";
      panel.hidden = false;
    })
    .catch(() => {
      panel.hidden = true;
    });
}

function staticChoiceExampleHtml(label, config, value) {
  const min = Number(config.min ?? 0);
  const max = Number(config.max ?? 5);
  const step = Number(config.step || 1);
  const options = [];
  for (let option = min; option <= max; option += step) {
    options.push(String(option));
  }
  return `
    <div class="example-choice-field">
      <div class="example-choice-title">
        <strong>${escapeHtml(label)}</strong>
        <output>${escapeHtml(value)}</output>
      </div>
      <div class="choice-row" aria-label="${escapeHtml(label)} example">
        ${options
          .map(
            (option) => `
              <span class="choice-button example-choice${String(value) === option ? " is-selected" : ""}">
                ${escapeHtml(option)}
              </span>
            `,
          )
          .join("")}
      </div>
      <div class="choice-meta">
        <span>${escapeHtml(config.minLabel || String(min))}</span>
        <span></span>
        <span>${escapeHtml(config.maxLabel || String(max))}</span>
      </div>
    </div>
  `;
}

function renderEvaluation() {
  state.flatQuestions = flattenQuestions(state.flow, state.videoOrder);
  if (!state.flatQuestions.length) {
    showAlert("The current workflow has no evaluation criteria.");
    return;
  }
  if (state.currentIndex < 0 || state.currentIndex >= state.flatQuestions.length) {
    state.currentIndex = firstIncompleteIndex();
  }
  const current = state.flatQuestions[state.currentIndex];
  hideTextReferenceMenu();
  state.videoText.pendingSelection = null;
  if (
    state.videoText.activeReferenceId
    && !answerReferences(state.answers[current.answerKey]).some(
      (reference) => reference.id === state.videoText.activeReferenceId,
    )
  ) {
    state.videoText.activeReferenceId = "";
  }
  document.getElementById("currentDimensionDescription").textContent = isCancelledItem(current)
    ? cancelledVideoMessage(current.video)
    : current.dimension.description || "";
  renderQuestionPrompt(current);
  document.getElementById("videoProgress").textContent = `Video ${current.videoIndex + 1}/${participantVideos().length}`;
  document.getElementById("dimensionProgress").textContent =
    `Dimension ${current.dimensionIndex + 1}/${(state.flow.dimensions || []).length}`;
  document.getElementById("subdimensionProgress").textContent =
    `Criterion ${current.questionIndex + 1}/${(current.dimension.questions || []).length}`;
  renderEvaluationSelectors(current);

  renderEvaluationVideo(current.video);
  renderVideoText(current);
  renderVideoTextMode(current);
  renderAnswerForm(current);
  applyEvaluationPolicyControls();
}

function renderEvaluationVideo(video) {
  const videoContainer = document.getElementById("videoContainer");
  const source = video?.fileName ? videoSource(video.fileName) : "";
  const existing = videoContainer.querySelector("video");
  if (!source) {
    removeVideoPreviewTimeline(videoContainer);
    videoContainer.textContent = "No filename is configured for this video.";
  } else if (!existing || existing.dataset.source !== source) {
    videoContainer.innerHTML = `<video controls preload="metadata" src="${escapeAttribute(source)}"></video>`;
    videoContainer.querySelector("video").dataset.source = source;
  }
  if (source) {
    attachVideoPreview(
      videoContainer.querySelector("video"),
      video.fileName,
      state.flow,
      { allowVideoReference: true, videoId: String(video.id || "") },
    );
  }
}

function expandVideoText() {
  syncCurrentAnswer();
  hideTextReferenceMenu();
  state.videoText.expanded = true;
  renderVideoTextMode(state.flatQuestions[state.currentIndex]);
}

function collapseVideoText() {
  hideTextReferenceMenu();
  state.videoText.expanded = false;
  renderVideoTextMode(state.flatQuestions[state.currentIndex]);
}

function toggleVideoTextLanguage() {
  if (state.videoText.language !== "translation" && !state.videoText.translationAvailable) {
    updateVideoTextLanguageControls();
    return;
  }
  hideTextReferenceMenu();
  state.videoText.activeReferenceId = "";
  state.videoText.language = state.videoText.language === "translation" ? "original" : "translation";
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  renderVideoText(current);
  renderVideoTextMode(current);
}

function renderVideoTextMode(current) {
  if (!current) return;
  const expanded = state.videoText.expanded;
  document.getElementById("videoTextPanel").hidden = expanded;
  document.getElementById("questionWorkPanel").hidden = expanded;
  document.getElementById("expandedVideoTextPanel").hidden = !expanded;
  document.getElementById("expandVideoText").textContent = expanded ? "Expanded on Right" : "Expand on Right";
  document.getElementById("expandVideoText").disabled = expanded;
  if (expanded) {
    document.getElementById("expandedVideoTextTitle").textContent = videoTextTitle(current.videoIndex);
    document.getElementById("expandedVideoTextMeta").textContent =
      "This transcript matches the video on the left. Scroll to read it, then select Return to Evaluation to continue.";
  }
  if (state.videoText.activeReferenceId) {
    requestAnimationFrame(() => scrollActiveTextReferenceIntoView(state.videoText.activeReferenceId));
  }
}

function renderVideoText(current) {
  const request = videoTextRequest(current.video);
  const title = document.getElementById("videoTextTitle");
  if (state.videoText.currentBaseKey !== request.baseKey) {
    state.videoText.currentBaseKey = request.baseKey;
    state.videoText.language = "original";
    state.videoText.translationAvailable = false;
  }
  title.textContent = videoTextTitle(current.videoIndex);
  updateVideoTextLanguageControls();
  const cacheKey = `${request.baseKey}::${state.videoText.language}`;
  state.videoText.currentPath = cacheKey;
  if (!request.videoPath) {
    setVideoTextContent("No filename is configured, so the transcript cannot be located automatically.");
    state.videoText.translationAvailable = false;
    updateVideoTextLanguageControls();
    return;
  }
  if (Object.prototype.hasOwnProperty.call(state.videoText.cache, cacheKey)) {
    const cached = state.videoText.cache[cacheKey];
    state.videoText.language = cached.language === "translation" ? "translation" : "original";
    state.videoText.translationAvailable = Boolean(cached.translationAvailable);
    setVideoTextContent(cached.text);
    updateVideoTextLanguageControls();
    return;
  }
  const loadingText = "Loading transcript...";
  setVideoTextContent(loadingText);
  const params = new URLSearchParams({
    video_path: request.videoPath,
    language: state.videoText.language,
  });
  if (request.textPath) params.set("text_path", request.textPath);
  fetch(`/api/video-text?${params.toString()}`)
    .then(async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      return payload;
    })
    .then((payload) => {
      const sourceText = String(payload.text || "");
      state.videoText.cache[cacheKey] = {
        text: sourceText || "The transcript file is empty.",
        translationAvailable: Boolean(payload.translationAvailable),
        language: payload.language === "translation" ? "translation" : "original",
        status: sourceText.trim() ? "ready" : "empty",
      };
      if (state.videoText.currentPath === cacheKey) {
        state.videoText.language = state.videoText.cache[cacheKey].language;
        state.videoText.translationAvailable = Boolean(payload.translationAvailable);
        setVideoTextContent(state.videoText.cache[cacheKey].text);
        updateVideoTextLanguageControls();
      }
    })
    .catch((error) => {
      const message =
        `Transcript loading failed: ${error.message || String(error)}\n\n` +
        "Place the text file beside the video and use a related filename, or set textFileName explicitly.";
      state.videoText.cache[cacheKey] = {
        text: message,
        translationAvailable: false,
        status: "error",
      };
      if (state.videoText.currentPath === cacheKey) {
        state.videoText.translationAvailable = false;
        setVideoTextContent(message);
        updateVideoTextLanguageControls();
      }
    });
}

function setVideoTextContent(text) {
  const reference = activeTextReference();
  for (const viewer of [
    document.getElementById("videoTextBox"),
    document.getElementById("expandedVideoTextBox"),
  ]) {
    renderVideoTextViewer(viewer, String(text || ""), reference);
  }
  if (reference) {
    requestAnimationFrame(() => scrollActiveTextReferenceIntoView(reference.id));
  }
}

function renderVideoTextViewer(viewer, text, reference) {
  viewer.replaceChildren();
  const range = reference && reference.source_key === state.videoText.currentPath
    ? locateTextReference(text, reference)
    : null;
  if (!range) {
    viewer.textContent = text;
    return;
  }
  viewer.append(document.createTextNode(text.slice(0, range.start)));
  const mark = document.createElement("mark");
  mark.className = "text-reference-highlight";
  mark.dataset.referenceId = reference.id;
  mark.textContent = text.slice(range.start, range.end);
  viewer.append(mark, document.createTextNode(text.slice(range.end)));
}

function locateTextReference(text, reference) {
  const start = Number(reference.start);
  const end = Number(reference.end);
  const quote = String(reference.text || "");
  if (Number.isInteger(start) && Number.isInteger(end) && text.slice(start, end) === quote) {
    return { start, end };
  }
  if (!quote) return null;
  const matches = [];
  let offset = text.indexOf(quote);
  while (offset >= 0) {
    matches.push(offset);
    offset = text.indexOf(quote, offset + Math.max(1, quote.length));
  }
  if (!matches.length) return null;
  const prefix = String(reference.prefix || "");
  const suffix = String(reference.suffix || "");
  const contextual = matches.find((candidate) => {
    const prefixMatches = !prefix || text.slice(Math.max(0, candidate - prefix.length), candidate) === prefix;
    const suffixMatches = !suffix || text.slice(candidate + quote.length, candidate + quote.length + suffix.length) === suffix;
    return prefixMatches && suffixMatches;
  });
  const resolvedStart = contextual ?? matches.reduce((best, candidate) =>
    Math.abs(candidate - start) < Math.abs(best - start) ? candidate : best,
  matches[0]);
  return { start: resolvedStart, end: resolvedStart + quote.length };
}

function activeTextReference() {
  const current = state.flatQuestions[state.currentIndex];
  if (!current || !state.videoText.activeReferenceId) return null;
  return answerReferences(state.answers[current.answerKey]).find(
    (reference) =>
      reference.id === state.videoText.activeReferenceId
      && isTextReference(reference),
  ) || null;
}

function scrollActiveTextReferenceIntoView(referenceId) {
  const viewer = document.getElementById(
    state.videoText.expanded ? "expandedVideoTextBox" : "videoTextBox",
  );
  const mark = [...(viewer?.querySelectorAll("[data-reference-id]") || [])].find(
    (item) => item.dataset.referenceId === referenceId,
  );
  if (!mark) return;
  mark.scrollIntoView({ block: "center", behavior: "smooth" });
  viewer.focus({ preventScroll: true });
}

function bindVideoTextReferenceActions() {
  const viewers = [
    document.getElementById("videoTextBox"),
    document.getElementById("expandedVideoTextBox"),
  ];
  for (const viewer of viewers) {
    viewer.addEventListener("mouseup", (event) => {
      setTimeout(() => captureVideoTextSelection(viewer, event.clientX, event.clientY), 0);
    });
    viewer.addEventListener("keyup", () => {
      setTimeout(() => captureVideoTextSelection(viewer), 0);
    });
    viewer.addEventListener("touchend", () => {
      setTimeout(() => captureVideoTextSelection(viewer), 0);
    });
    viewer.addEventListener("contextmenu", (event) => {
      if (!captureVideoTextSelection(viewer, event.clientX, event.clientY)) return;
      event.preventDefault();
    });
    viewer.addEventListener("scroll", hideTextReferenceMenu);
  }

  document.getElementById("addTextReference").addEventListener("click", addPendingTextReference);
  document.getElementById("copySelectedVideoText").addEventListener("click", copyPendingVideoText);
  document.addEventListener("click", (event) => {
    const menu = document.getElementById("textReferenceMenu");
    if (menu.contains(event.target) || event.target.closest?.(".video-text-box")) return;
    hideTextReferenceMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideTextReferenceMenu();
  });
  window.addEventListener("resize", hideTextReferenceMenu);
}

function captureVideoTextSelection(viewer, clientX = null, clientY = null) {
  const current = state.flatQuestions[state.currentIndex];
  const cached = state.videoText.cache[state.videoText.currentPath];
  if (!current || isCancelledItem(current) || cached?.status !== "ready") {
    hideTextReferenceMenu();
    return false;
  }
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) {
    hideTextReferenceMenu();
    return false;
  }
  const range = selection.getRangeAt(0);
  if (!viewer.contains(range.startContainer) || !viewer.contains(range.endContainer)) {
    hideTextReferenceMenu();
    return false;
  }

  const before = range.cloneRange();
  before.selectNodeContents(viewer);
  before.setEnd(range.startContainer, range.startOffset);
  let start = before.toString().length;
  let end = start + range.toString().length;
  const sourceText = String(cached.text || "");
  let quote = sourceText.slice(start, end);
  const leadingWhitespace = quote.match(/^\s*/u)?.[0].length || 0;
  const trailingWhitespace = quote.match(/\s*$/u)?.[0].length || 0;
  start += leadingWhitespace;
  end -= trailingWhitespace;
  quote = sourceText.slice(start, end);
  if (!quote.trim()) {
    hideTextReferenceMenu();
    return false;
  }

  state.videoText.pendingSelection = {
    video_id: String(current.video.id),
    language: state.videoText.language,
    source_key: state.videoText.currentPath,
    start,
    end,
    source_length: sourceText.length,
    text: quote,
    prefix: sourceText.slice(Math.max(0, start - 40), start),
    suffix: sourceText.slice(end, Math.min(sourceText.length, end + 40)),
  };
  const rect = range.getBoundingClientRect();
  showTextReferenceMenu(
    clientX ?? rect.left + rect.width / 2,
    clientY ?? rect.bottom,
  );
  return true;
}

function showTextReferenceMenu(clientX, clientY) {
  const menu = document.getElementById("textReferenceMenu");
  menu.hidden = false;
  menu.style.left = "0px";
  menu.style.top = "0px";
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.max(8, Math.min(Number(clientX) || 8, window.innerWidth - width - 8));
  const top = Math.max(8, Math.min((Number(clientY) || 8) + 8, window.innerHeight - height - 8));
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}

function hideTextReferenceMenu() {
  const menu = document.getElementById("textReferenceMenu");
  if (menu) menu.hidden = true;
}

function addPendingTextReference() {
  const selection = state.videoText.pendingSelection;
  const current = state.flatQuestions[state.currentIndex];
  if (!selection || !current || isCancelledItem(current)) {
    hideTextReferenceMenu();
    return;
  }
  if (
    selection.video_id !== String(current.video.id)
    || selection.source_key !== state.videoText.currentPath
  ) {
    hideTextReferenceMenu();
    showAlert("The transcript or evaluation item changed. Select the text again.");
    return;
  }
  const quoteLength = textCharacterCount(selection.text);
  if (quoteLength > TEXT_REFERENCE_MAX_CHARS) {
    showAlert(`A reference may contain at most ${TEXT_REFERENCE_MAX_CHARS} characters; ${quoteLength} were selected.`);
    return;
  }

  const previous = state.answers[current.answerKey] || {};
  const references = answerReferences(previous);
  const duplicate = references.find(
    (reference) =>
      reference.source_key === selection.source_key
      && Number(reference.start) === selection.start
      && Number(reference.end) === selection.end
      && reference.text === selection.text,
  );
  if (duplicate) {
    state.videoText.activeReferenceId = duplicate.id;
    hideTextReferenceMenu();
    setVideoTextContent(state.videoText.cache[state.videoText.currentPath]?.text || "");
    return;
  }
  const currentTotal = references.filter(isTextReference).reduce(
    (total, reference) => total + textCharacterCount(reference.text),
    0,
  );
  if (currentTotal + quoteLength > TEXT_REFERENCE_TOTAL_MAX_CHARS) {
    showAlert(
      `References for this criterion may total at most ${TEXT_REFERENCE_TOTAL_MAX_CHARS} characters. `
      + `${currentTotal} are already quoted and ${quoteLength} are selected.`,
    );
    return;
  }

  const reference = {
    ...selection,
    id: createTextReferenceId(),
  };
  const nextReferences = [...references, reference];
  const explanationEditor = document.getElementById("explanationInput");
  let explanationBody = answerExplanationBody(previous);
  let referencePlacements = answerReferencePlacements(
    previous,
    references,
    explanationBody,
  );
  if (
    explanationEditor
    && explanationEditor.dataset.answerKey === current.answerKey
  ) {
    insertAnswerReferenceAtSavedSelection(
      current,
      explanationEditor,
      reference,
      nextReferences.length - 1,
    );
    const editorValue = readExplanationEditor(explanationEditor, nextReferences);
    explanationBody = editorValue.body;
    referencePlacements = editorValue.placements;
  } else {
    referencePlacements.push({
      reference_id: reference.id,
      offset: textCharacterCount(explanationBody),
    });
  }
  const nextAnswer = {
    ...previous,
    explanation_body: explanationBody,
    references: nextReferences,
    reference_placements: referencePlacements,
  };
  nextAnswer.explanation = composeAnswerExplanation(
    nextReferences,
    nextAnswer.explanation_body,
    nextAnswer.reference_placements,
  );
  state.answers[current.answerKey] = nextAnswer;
  state.videoText.activeReferenceId = reference.id;
  if (
    explanationEditor
    && explanationEditor.dataset.answerKey === current.answerKey
  ) {
    renderExplanationEditor(explanationEditor, nextAnswer);
    const insertedChip = [...explanationEditor.querySelectorAll("[data-answer-reference-id]")]
      .find((chip) => chip.dataset.answerReferenceId === reference.id);
    if (insertedChip) {
      const caretRange = document.createRange();
      caretRange.setStartAfter(insertedChip);
      caretRange.collapse(true);
      setExplanationEditorRange(current, explanationEditor, caretRange);
    }
  }
  markCurrentAnswerDirty(current.answerKey, previous, nextAnswer);
  hideTextReferenceMenu();
  updateExplanationReferenceActiveState();
  setVideoTextContent(state.videoText.cache[state.videoText.currentPath]?.text || "");
  const referenceLabel = `Reference ${String(nextReferences.length).padStart(2, "0")}`;
  showAlert(
    state.videoText.expanded
      ? `${referenceLabel} added. Return to the evaluation to view it in the notes field.`
      : `${referenceLabel} added at the current cursor position.`,
    false,
  );
}

function addVideoTimeReference(videoId, requestedTimeSeconds) {
  const current = state.flatQuestions[state.currentIndex];
  const timeSeconds = Math.max(0, Math.floor(Number(requestedTimeSeconds)));
  if (
    !current
    || isCancelledItem(current)
    || String(current.video.id || "") !== String(videoId || "")
    || !Number.isFinite(timeSeconds)
  ) {
    showAlert("The video or evaluation item changed. Select the timestamp again.");
    return;
  }

  const previous = state.answers[current.answerKey] || {};
  const references = answerReferences(previous);
  const duplicate = references.find(
    (reference) =>
      isVideoTimeReference(reference)
      && String(reference.video_id) === String(videoId)
      && Number(reference.time_seconds) === timeSeconds,
  );
  if (duplicate) {
    state.videoText.activeReferenceId = duplicate.id;
    activateAnswerReference(duplicate.id);
    showAlert(`Video time ${formatVideoPreviewTime(timeSeconds)} was quoted in the current notes.`, false);
    return;
  }

  const reference = {
    id: createTextReferenceId(),
    type: "video_time",
    video_id: String(videoId),
    time_seconds: timeSeconds,
  };
  const nextReferences = [...references, reference];
  const explanationEditor = document.getElementById("explanationInput");
  let explanationBody = answerExplanationBody(previous);
  let referencePlacements = answerReferencePlacements(
    previous,
    references,
    explanationBody,
  );
  if (
    explanationEditor
    && explanationEditor.dataset.answerKey === current.answerKey
  ) {
    insertAnswerReferenceAtSavedSelection(
      current,
      explanationEditor,
      reference,
      nextReferences.length - 1,
    );
    const editorValue = readExplanationEditor(explanationEditor, nextReferences);
    explanationBody = editorValue.body;
    referencePlacements = editorValue.placements;
  } else {
    referencePlacements.push({
      reference_id: reference.id,
      offset: textCharacterCount(explanationBody),
    });
  }
  const nextAnswer = {
    ...previous,
    explanation_body: explanationBody,
    references: nextReferences,
    reference_placements: referencePlacements,
  };
  nextAnswer.explanation = composeAnswerExplanation(
    nextReferences,
    nextAnswer.explanation_body,
    nextAnswer.reference_placements,
  );
  state.answers[current.answerKey] = nextAnswer;
  state.videoText.activeReferenceId = reference.id;
  if (
    explanationEditor
    && explanationEditor.dataset.answerKey === current.answerKey
  ) {
    renderExplanationEditor(explanationEditor, nextAnswer);
    const insertedChip = [...explanationEditor.querySelectorAll("[data-answer-reference-id]")]
      .find((chip) => chip.dataset.answerReferenceId === reference.id);
    if (insertedChip) {
      const caretRange = document.createRange();
      caretRange.setStartAfter(insertedChip);
      caretRange.collapse(true);
      setExplanationEditorRange(current, explanationEditor, caretRange);
    }
  }
  markCurrentAnswerDirty(current.answerKey, previous, nextAnswer);
  setVideoTextContent(state.videoText.cache[state.videoText.currentPath]?.text || "");
  updateExplanationReferenceActiveState();
  showAlert(
    `Added reference ${String(nextReferences.length).padStart(2, "0")}: video time `
      + formatVideoPreviewTime(timeSeconds),
    false,
  );
}

async function copyPendingVideoText() {
  const text = String(state.videoText.pendingSelection?.text || "");
  hideTextReferenceMenu();
  if (!text) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    throw new Error("The clipboard API is unavailable in this browser")
  } catch (_error) {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
}

function createTextReferenceId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `ref-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function textCharacterCount(value) {
  return Array.from(String(value || "")).length;
}

function isVideoTimeReference(reference) {
  return (
    reference
    && typeof reference === "object"
    && String(reference.type || "").trim().toLowerCase() === "video_time"
    && String(reference.id || "").trim()
    && String(reference.video_id || "").trim()
    && Number.isInteger(reference.time_seconds)
    && reference.time_seconds >= 0
  );
}

function isTextReference(reference) {
  if (!reference || typeof reference !== "object") return false;
  const referenceType = String(reference.type || "text").trim().toLowerCase() || "text";
  return (
    referenceType === "text"
    && String(reference.id || "").trim()
    && String(reference.text || "").trim()
  );
}

function answerReferences(answer) {
  if (!Array.isArray(answer?.references)) return [];
  return answer.references.filter(
    (reference) => isTextReference(reference) || isVideoTimeReference(reference),
  );
}

function answerExplanationBody(answer) {
  if (!answer || typeof answer !== "object") return "";
  if (Object.prototype.hasOwnProperty.call(answer, "explanation_body")) {
    return String(answer.explanation_body || "");
  }
  return answerReferences(answer).length ? "" : String(answer.explanation || "");
}

function answerReferencePlacements(answer, references, explanationBody) {
  const bodyLength = textCharacterCount(explanationBody);
  const knownIds = new Set(references.map((reference) => String(reference.id || "")));
  const seenIds = new Set();
  const placements = [];
  const storedPlacements = Array.isArray(answer?.reference_placements)
    ? answer.reference_placements
    : [];
  for (const placement of storedPlacements) {
    const referenceId = String(placement?.reference_id || "");
    const offset = Number(placement?.offset);
    if (
      !knownIds.has(referenceId)
      || seenIds.has(referenceId)
      || !Number.isInteger(offset)
      || offset < 0
      || offset > bodyLength
    ) {
      continue;
    }
    placements.push({ reference_id: referenceId, offset });
    seenIds.add(referenceId);
  }
  for (const reference of references) {
    const referenceId = String(reference.id || "");
    if (!seenIds.has(referenceId)) {
      placements.push({ reference_id: referenceId, offset: bodyLength });
    }
  }
  return placements
    .map((placement, order) => ({ ...placement, order }))
    .sort((left, right) => left.offset - right.offset || left.order - right.order)
    .map(({ reference_id: referenceId, offset }) => ({
      reference_id: referenceId,
      offset,
    }));
}

function textReferencePositionPercent(reference) {
  const start = Math.max(0, Number(reference?.start) || 0);
  const storedSourceLength = Number(reference?.source_length);
  const fallbackSourceLength = Math.max(
    Number(reference?.end) || 0,
    (Number(reference?.end) || 0) + String(reference?.suffix || "").length,
  );
  const sourceLength = Number.isInteger(storedSourceLength) && storedSourceLength > 0
    ? storedSourceLength
    : fallbackSourceLength;
  if (sourceLength <= 0) return 0;
  return Math.round(Math.min(start, sourceLength) * 100 / sourceLength);
}

function formatTextReferenceForResult(reference) {
  const position = textReferencePositionPercent(reference);
  return `'''[${position}%]: ${String(reference?.text || "")}'''`;
}

function formatAnswerReferenceForResult(reference) {
  if (isVideoTimeReference(reference)) {
    return `'''Video[${formatVideoPreviewTime(reference.time_seconds)}]'''`;
  }
  return formatTextReferenceForResult(reference);
}

function composeAnswerExplanation(references, explanationBody, referencePlacements = []) {
  const body = String(explanationBody || "");
  if (!references.length) return body.trim();
  const characters = Array.from(body);
  const referencesById = new Map(
    references.map((reference) => [reference.id, reference]),
  );
  const placements = answerReferencePlacements(
    { reference_placements: referencePlacements },
    references,
    body,
  );
  let result = "";
  let bodyOffset = 0;
  for (const placement of placements) {
    if (placement.offset > bodyOffset) {
      result += characters.slice(bodyOffset, placement.offset).join("");
    }
    const reference = referencesById.get(placement.reference_id);
    if (reference) result += formatAnswerReferenceForResult(reference);
    bodyOffset = placement.offset;
  }
  result += characters.slice(bodyOffset).join("");
  return result.trim();
}

function textReferencePreview(reference, index) {
  const normalized = String(reference.text || "").replace(/\s+/gu, " ").trim();
  const characters = Array.from(normalized);
  const preview = characters.slice(0, TEXT_REFERENCE_PREVIEW_CHARS).join("");
  const suffix = characters.length > TEXT_REFERENCE_PREVIEW_CHARS ? "..." : "";
  return `Reference ${String(index + 1).padStart(2, "0")}--${preview}${suffix}`;
}

function answerReferencePreview(reference, index) {
  if (isVideoTimeReference(reference)) {
    return `Reference ${String(index + 1).padStart(2, "0")}: Video ${formatVideoPreviewTime(
      reference.time_seconds,
    )}`;
  }
  return textReferencePreview(reference, index);
}

function createAnswerReferenceChip(reference, index, { readOnly = false } = {}) {
  const chip = document.createElement("span");
  chip.className = "answer-reference-chip";
  chip.classList.toggle("is-readonly", readOnly);
  chip.classList.toggle("is-video-reference", isVideoTimeReference(reference));
  if (reference.id === state.videoText.activeReferenceId) {
    chip.classList.add("is-active");
  }
  chip.contentEditable = "false";
  chip.dataset.answerReferenceId = reference.id;

  const referenceLabel = answerReferencePreview(reference, index);
  const actionDescription = isVideoTimeReference(reference)
    ? "Jump to this video time"
    : "Highlight the quoted text";
  chip.title = isVideoTimeReference(reference)
    ? `Video ${formatVideoPreviewTime(reference.time_seconds)}`
    : reference.text;
  if (readOnly) {
    chip.textContent = isVideoTimeReference(reference)
      ? referenceLabel
      : `Reference ${String(index + 1).padStart(2, "0")}: "${reference.text}"`;
  } else {
    const highlight = document.createElement("button");
    highlight.type = "button";
    highlight.className = "answer-reference-highlight";
    highlight.dataset.highlightReference = reference.id;
    highlight.setAttribute("aria-label", `${referenceLabel}, ${actionDescription}`);
    highlight.textContent = referenceLabel;

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "answer-reference-remove";
    remove.dataset.removeReference = reference.id;
    remove.setAttribute("aria-label", `Delete ${referenceLabel}`);
    remove.title = "Delete reference";
    remove.textContent = "\u00d7";

    chip.append(highlight, remove);
  }
  return chip;
}

function renderAnswerReferenceContent(container, answer, { readOnly = false } = {}) {
  const references = answerReferences(answer);
  const body = answerExplanationBody(answer);
  const characters = Array.from(body);
  const referencesById = new Map(
    references.map((reference, index) => [reference.id, { reference, index }]),
  );
  const placements = answerReferencePlacements(answer, references, body);
  const fragment = document.createDocumentFragment();
  let bodyOffset = 0;
  for (const placement of placements) {
    if (placement.offset > bodyOffset) {
      fragment.append(document.createTextNode(
        characters.slice(bodyOffset, placement.offset).join(""),
      ));
    }
    const entry = referencesById.get(placement.reference_id);
    if (entry) {
      fragment.append(createAnswerReferenceChip(entry.reference, entry.index, { readOnly }));
    }
    bodyOffset = placement.offset;
  }
  if (bodyOffset < characters.length) {
    fragment.append(document.createTextNode(characters.slice(bodyOffset).join("")));
  }
  container.replaceChildren(fragment);
}

function renderExplanationEditor(editor, answer) {
  renderAnswerReferenceContent(editor, answer);
}

function renderResultExplanation(container, answer) {
  const references = answerReferences(answer);
  const hasStructuredAnswer =
    references.length > 0
    || Object.prototype.hasOwnProperty.call(answer || {}, "explanation_body");
  const explanation = hasStructuredAnswer
    ? composeAnswerExplanation(
        references,
        answerExplanationBody(answer),
        answerReferencePlacements(answer, references, answerExplanationBody(answer)),
      )
    : String(answer?.explanation || "").trim();
  container.textContent = explanation || "No response";
}

function readExplanationEditor(editor, references) {
  let body = "";
  const placements = [];
  const knownIds = new Set(references.map((reference) => String(reference.id || "")));
  const seenIds = new Set();
  const blockTags = new Set(["DIV", "P", "LI"]);

  const appendText = (value) => {
    body += String(value || "").replace(/\r\n?/gu, "\n");
  };
  const visit = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      appendText(node.nodeValue);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const referenceId = String(node.dataset?.answerReferenceId || "");
    if (referenceId) {
      if (knownIds.has(referenceId) && !seenIds.has(referenceId)) {
        placements.push({
          reference_id: referenceId,
          offset: textCharacterCount(body),
        });
        seenIds.add(referenceId);
      }
      return;
    }
    if (node.tagName === "BR") {
      appendText("\n");
      return;
    }
    const isBlock = node !== editor && blockTags.has(node.tagName);
    if (isBlock && body && !body.endsWith("\n")) appendText("\n");
    for (const child of node.childNodes) visit(child);
    if (isBlock && node.nextSibling && !body.endsWith("\n")) appendText("\n");
  };

  for (const child of editor.childNodes) visit(child);
  return { body, placements };
}

function mergeExplanationEditorPlacements(previousAnswer, references, editorValue) {
  const bodyLength = textCharacterCount(editorValue.body);
  const detectedById = new Map(
    editorValue.placements.map((placement) => [
      String(placement.reference_id || ""),
      placement,
    ]),
  );
  const previousBody = answerExplanationBody(previousAnswer);
  const previousPlacements = answerReferencePlacements(
    previousAnswer,
    references,
    previousBody,
  );
  const mergedPlacements = previousPlacements.map((placement) => {
    const detected = detectedById.get(placement.reference_id);
    if (detected) return detected;
    return {
      reference_id: placement.reference_id,
      offset: Math.min(placement.offset, bodyLength),
    };
  });
  const previouslyPlacedIds = new Set(
    previousPlacements.map((placement) => placement.reference_id),
  );
  for (const placement of editorValue.placements) {
    if (!previouslyPlacedIds.has(placement.reference_id)) {
      mergedPlacements.push(placement);
    }
  }
  return {
    placements: answerReferencePlacements(
      { reference_placements: mergedPlacements },
      references,
      editorValue.body,
    ),
    missingReferenceIds: references
      .map((reference) => String(reference.id || ""))
      .filter((referenceId) => !detectedById.has(referenceId)),
  };
}

function explanationEditorCaretBodyOffset(editor, references) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1) return null;
  const selectionRange = selection.getRangeAt(0);
  if (!rangeBelongsToEditor(selectionRange, editor)) return null;
  const prefixRange = selectionRange.cloneRange();
  prefixRange.selectNodeContents(editor);
  prefixRange.setEnd(selectionRange.endContainer, selectionRange.endOffset);
  const prefix = document.createElement("div");
  prefix.append(prefixRange.cloneContents());
  return textCharacterCount(readExplanationEditor(prefix, references).body);
}

function explanationEditorRangeAtBodyOffset(editor, requestedOffset) {
  let remaining = Math.max(0, Number(requestedOffset) || 0);
  let candidate = document.createRange();
  candidate.setStart(editor, 0);
  candidate.collapse(true);

  const setAfter = (node) => {
    const range = document.createRange();
    range.setStartAfter(node);
    range.collapse(true);
    candidate = range;
  };
  const visit = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const characters = Array.from(node.nodeValue || "");
      if (remaining < characters.length) {
        const range = document.createRange();
        const utf16Offset = characters.slice(0, remaining).join("").length;
        range.setStart(node, utf16Offset);
        range.collapse(true);
        candidate = range;
        return true;
      }
      remaining -= characters.length;
      setAfter(node);
      return false;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return false;
    if (node.dataset?.answerReferenceId) {
      if (remaining === 0) setAfter(node);
      return false;
    }
    for (const child of node.childNodes) {
      if (visit(child)) return true;
    }
    return false;
  };

  for (const child of editor.childNodes) {
    if (visit(child)) break;
  }
  return candidate;
}

function rangeBelongsToEditor(range, editor) {
  const inside = (node) => node === editor || editor.contains(node);
  return Boolean(range && inside(range.startContainer) && inside(range.endContainer));
}

function rememberExplanationEditorSelection(current, editor) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1) return;
  const range = selection.getRangeAt(0);
  if (!rangeBelongsToEditor(range, editor)) return;
  state.explanationEditorSelection = {
    answerKey: current.answerKey,
    editor,
    range: range.cloneRange(),
  };
}

function defaultExplanationEditorRange(editor) {
  const range = document.createRange();
  range.selectNodeContents(editor);
  range.collapse(false);
  return range;
}

function savedExplanationEditorRange(current, editor) {
  const saved = state.explanationEditorSelection;
  if (
    saved?.answerKey === current.answerKey
    && saved.editor === editor
    && rangeBelongsToEditor(saved.range, editor)
  ) {
    return saved.range.cloneRange();
  }
  return defaultExplanationEditorRange(editor);
}

function setExplanationEditorRange(current, editor, range, focusEditor = true) {
  if (focusEditor && !editor.closest("[hidden]")) {
    editor.focus({ preventScroll: true });
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
  state.explanationEditorSelection = {
    answerKey: current.answerKey,
    editor,
    range: range.cloneRange(),
  };
}

function insertAnswerReferenceAtSavedSelection(current, editor, reference, index) {
  const range = savedExplanationEditorRange(current, editor);
  range.collapse(false);
  const chip = createAnswerReferenceChip(reference, index);
  range.insertNode(chip);
  const caretRange = document.createRange();
  caretRange.setStartAfter(chip);
  caretRange.collapse(true);
  setExplanationEditorRange(current, editor, caretRange);
}

function insertPlainTextIntoExplanationEditor(current, editor, text) {
  const normalized = String(text || "").replace(/\r\n?/gu, "\n");
  if (!normalized) return;
  const range = savedExplanationEditorRange(current, editor);
  range.deleteContents();
  const textNode = document.createTextNode(normalized);
  range.insertNode(textNode);
  const caretRange = document.createRange();
  caretRange.setStart(textNode, textNode.length);
  caretRange.collapse(true);
  setExplanationEditorRange(current, editor, caretRange);
  editor.dispatchEvent(new Event("input", { bubbles: true }));
}

function referenceChipAdjacentToCaret(editor, key) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1) return null;
  const range = selection.getRangeAt(0);
  if (!range.collapsed || !rangeBelongsToEditor(range, editor)) return null;
  const backward = key === "Backspace";
  let current = range.startContainer;
  const offset = range.startOffset;

  if (current.nodeType === Node.TEXT_NODE) {
    const textLength = current.nodeValue?.length || 0;
    if ((backward && offset > 0) || (!backward && offset < textLength)) return null;
  } else if (current.nodeType === Node.ELEMENT_NODE) {
    const childIndex = backward ? offset - 1 : offset;
    if (childIndex >= 0 && childIndex < current.childNodes.length) {
      current = current.childNodes[childIndex];
      while (
        current.nodeType === Node.ELEMENT_NODE
        && !current.dataset?.answerReferenceId
        && current.childNodes.length
      ) {
        current = backward
          ? current.childNodes[current.childNodes.length - 1]
          : current.childNodes[0];
      }
      const element = current.nodeType === Node.ELEMENT_NODE
        ? current
        : current.parentElement;
      return element?.closest?.("[data-answer-reference-id]") || null;
    }
  } else {
    return null;
  }

  while (current && current !== editor) {
    const parent = current.parentNode;
    if (!parent) return null;
    const siblings = [...parent.childNodes];
    const index = siblings.indexOf(current);
    const siblingIndex = backward ? index - 1 : index + 1;
    if (siblingIndex >= 0 && siblingIndex < siblings.length) {
      current = siblings[siblingIndex];
      while (
        current.nodeType === Node.ELEMENT_NODE
        && !current.dataset?.answerReferenceId
        && current.childNodes.length
      ) {
        current = backward
          ? current.childNodes[current.childNodes.length - 1]
          : current.childNodes[0];
      }
      const element = current.nodeType === Node.ELEMENT_NODE
        ? current
        : current.parentElement;
      const chip = element?.closest?.("[data-answer-reference-id]");
      return chip && editor.contains(chip) ? chip : null;
    }
    current = parent;
  }
  return null;
}

function bindExplanationEditor(current, editor) {
  for (const eventName of ["focus", "mouseup", "keyup"]) {
    editor.addEventListener(eventName, () => {
      rememberExplanationEditorSelection(current, editor);
    });
  }
  editor.addEventListener("input", () => {
    rememberExplanationEditorSelection(current, editor);
    syncCurrentAnswer();
  });
  editor.addEventListener("keydown", (event) => {
    const isDeleteKey = ["Delete", "Backspace"].includes(event.key);
    const focusedChip = event.target.closest?.("[data-answer-reference-id]");
    const adjacentChip = isDeleteKey
      ? referenceChipAdjacentToCaret(editor, event.key)
      : null;
    const chipToRemove = focusedChip || adjacentChip;
    if (isDeleteKey && chipToRemove) {
      event.preventDefault();
      removeAnswerReference(current, chipToRemove.dataset.answerReferenceId);
      return;
    }
    const action = event.target.closest?.("[data-highlight-reference]");
    if (action && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      action.click();
      return;
    }
    if (event.target === editor && event.key === "Enter") {
      event.preventDefault();
      insertPlainTextIntoExplanationEditor(current, editor, "\n");
    }
  });
  editor.addEventListener("paste", (event) => {
    if (event.target !== editor) return;
    event.preventDefault();
    insertPlainTextIntoExplanationEditor(
      current,
      editor,
      event.clipboardData?.getData("text/plain") || "",
    );
  });
  editor.addEventListener("click", (event) => {
    const remove = event.target.closest?.("[data-remove-reference]");
    if (remove) {
      event.preventDefault();
      removeAnswerReference(current, remove.dataset.removeReference);
      return;
    }
    const highlight = event.target.closest?.("[data-highlight-reference]");
    if (highlight) {
      event.preventDefault();
      activateAnswerReference(highlight.dataset.highlightReference);
    }
  });

  const initialRange = defaultExplanationEditorRange(editor);
  state.explanationEditorSelection = {
    answerKey: current.answerKey,
    editor,
    range: initialRange,
  };
}

function updateExplanationReferenceActiveState() {
  document.querySelectorAll("[data-answer-reference-id]").forEach((chip) => {
    chip.classList.toggle(
      "is-active",
      chip.dataset.answerReferenceId === state.videoText.activeReferenceId,
    );
  });
}

function videoTextTitle(videoIndex) {
  const label = state.videoText.language === "translation" ? "Translation" : "Original";
  return `Video ${videoIndex + 1} | Transcript | ${label}`;
}

function updateVideoTextLanguageControls() {
  const visible = state.videoText.translationAvailable;
  const label = state.videoText.language === "translation" ? "View Original" : "View Translation";
  for (const button of [
    document.getElementById("toggleVideoTextLanguage"),
    document.getElementById("expandedToggleVideoTextLanguage"),
  ]) {
    button.hidden = !visible;
    button.disabled = !visible;
    button.textContent = label;
  }
}

function videoTextRequest(video) {
  const videoPath = mediaRelativePath(video?.fileName);
  const textPath = videoTextOverridePath(video);
  return {
    videoPath,
    textPath,
    baseKey: `${videoPath || ""}::${textPath || ""}`,
  };
}

function videoTextOverridePath(video) {
  const configured = video?.textFileName || video?.textPath || video?.textFile;
  if (!configured) return "";
  const configuredPath = cleanMediaPath(configured);
  if (!configuredPath || /^https?:\/\//.test(configuredPath) || configuredPath.includes("/")) {
    return mediaRelativePath(configuredPath);
  }
  const fileName = cleanMediaPath(video?.fileName);
  const directory = fileName.includes("/") ? fileName.slice(0, fileName.lastIndexOf("/") + 1) : "";
  return mediaRelativePath(`${directory}${configuredPath}`);
}

function renderEvaluationSelectors(current) {
  renderVideoSelect(current);
  renderDimensionSelect(current);
  renderQuestionSelect(current);
  updateNavigationButtons(current);
}

function renderVideoSelect(current) {
  const unlockedIndex = maxUnlockedVideoIndex();
  renderDropdown({
    buttonId: "videoSelectButton",
    menuId: "videoSelectMenu",
    currentIndex: current.videoIndex,
    items: participantVideos().map((video, videoIndex) => {
      const cancelled = isCancelledVideo(video);
      const locked = videoIndex > unlockedIndex;
      return {
        label: `Video ${videoIndex + 1}`,
        complete: isVideoComplete(videoIndex),
        disabled: locked,
        status: locked ? "Locked" : cancelled ? "Cancelled" : null,
        statusClass: locked ? "is-locked" : cancelled ? "is-cancelled" : null,
        onSelect: () => selectVideo(videoIndex),
      };
    }),
  });
}

function renderDimensionSelect(current) {
  renderDropdown({
    buttonId: "dimensionSelectButton",
    menuId: "dimensionSelectMenu",
    currentIndex: current.dimensionIndex,
    items: (state.flow.dimensions || []).map((dimension, dimensionIndex) => ({
      label: dimension.title,
      complete: isDimensionComplete(current.videoIndex, dimensionIndex),
      status: isCancelledVideo(current.video) ? "Cancelled" : null,
      statusClass: isCancelledVideo(current.video) ? "is-cancelled" : null,
      onSelect: () => selectDimension(dimensionIndex),
    })),
  });
}

function renderQuestionSelect(current) {
  const questions = current.dimension.questions || [];
  renderDropdown({
    buttonId: "questionSelectButton",
    menuId: "questionSelectMenu",
    currentIndex: current.questionIndex,
    items: questions.map((question, questionIndex) => ({
      label: question.prompt,
      complete: isQuestionComplete(current.videoIndex, current.dimensionIndex, questionIndex),
      status: isCancelledVideo(current.video) ? "Cancelled" : null,
      statusClass: isCancelledVideo(current.video) ? "is-cancelled" : null,
      onSelect: () => selectQuestion(questionIndex),
    })),
  });
}

function renderDropdown({ buttonId, menuId, currentIndex, items }) {
  const button = document.getElementById(buttonId);
  const menu = document.getElementById(menuId);
  const current = items[currentIndex] || { label: "Not configured", complete: false };
  button.innerHTML = dropdownItemHtml(current.label, current.complete, current.status, current.statusClass);
  button.dataset.complete = current.complete ? "true" : "false";
  menu.innerHTML = "";
  items.forEach((item, index) => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "dropdown-option";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", index === currentIndex ? "true" : "false");
    option.disabled = Boolean(item.disabled);
    option.title = item.disabled ? "Complete all answers for earlier videos first" : "";
    option.innerHTML = dropdownItemHtml(item.label, item.complete, item.status, item.statusClass);
    option.addEventListener("click", (event) => {
      event.stopPropagation();
      closeDropdown(menuId);
      item.onSelect();
    });
    menu.append(option);
  });
}

function dropdownItemHtml(label, complete, status = null, statusClass = null) {
  const statusText = status || (complete ? "Complete" : "Incomplete");
  const resolvedStatusClass = statusClass || (status ? "is-locked" : complete ? "is-complete" : "is-incomplete");
  return `
    <span class="dropdown-label">${escapeHtml(label)}</span>
    <span class="dropdown-status ${resolvedStatusClass}">
      ${escapeHtml(statusText)}
    </span>
  `;
}

function renderQuestionPrompt(current) {
  const container = document.getElementById("questionPrompt");
  renderQuestionPromptInto(container, current.question, state.flow);
}

function renderQuestionPromptInto(container, question, flow = state.flow) {
  container.innerHTML = "";
  if (!question) return;

  const description = question.description || question.specificDescription || "";
  if (description) {
    const descriptionBox = document.createElement("div");
    descriptionBox.className = "subdimension-description";
    const label = document.createElement("span");
    label.textContent = "Criterion Description";
    const text = document.createElement("p");
    text.textContent = description;
    descriptionBox.append(label, text);
    container.append(descriptionBox);
  }

  const rubric = normalizeRubric(question.scoreRubric || question.rubric || flow?.responseConfig?.scoreRubric);
  if (rubric.length) {
    const details = document.createElement("details");
    details.className = "rubric-details";
    const summary = document.createElement("summary");
    summary.textContent = "0-5 Scoring Rubric";
    details.append(summary);

    const list = document.createElement("ol");
    list.className = "rubric-list";
    for (const item of rubric) {
      const row = document.createElement("li");
      const score = document.createElement("strong");
      score.textContent = `${item.score} points`;
      const text = document.createElement("span");
      text.textContent = item.description;
      row.append(score, text);
      list.append(row);
    }
    details.append(list);
    container.append(details);
  }
}

function normalizeRubric(value) {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === "string") {
        const scoreMatch = item.match(/^(\d+)\s*[:. ]\s*(.*)$/);
        return scoreMatch ? { score: scoreMatch[1], description: scoreMatch[2] } : null;
      }
      if (!item || typeof item !== "object") return null;
      return {
        score: String(item.score ?? ""),
        description: String(item.description ?? item.label ?? ""),
      };
    })
    .filter((item) => item && item.score !== "" && item.description !== "");
}

function bindDropdownToggle(buttonId, menuId) {
  const button = document.getElementById(buttonId);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = document.getElementById(menuId);
    const willOpen = menu.hidden;
    closeAllDropdowns();
    menu.hidden = !willOpen;
    button.setAttribute("aria-expanded", willOpen ? "true" : "false");
  });
}

function closeDropdown(menuId) {
  const menu = document.getElementById(menuId);
  const button = document.getElementById(menuId.replace("Menu", "Button"));
  if (menu) menu.hidden = true;
  if (button) button.setAttribute("aria-expanded", "false");
}

function closeAllDropdowns() {
  ["videoSelectMenu", "dimensionSelectMenu", "questionSelectMenu"].forEach((menuId) => {
    const menu = document.getElementById(menuId);
    const button = document.getElementById(menuId.replace("Menu", "Button"));
    if (menu) menu.hidden = true;
    if (button) button.setAttribute("aria-expanded", "false");
  });
}

function closeDropdownsOnOutsideClick(event) {
  if (!event.target.closest(".custom-select")) {
    closeAllDropdowns();
  }
}

function updateNavigationButtons(current) {
  const videos = participantVideos();
  const unlockedIndex = maxUnlockedVideoIndex();
  const prevVideoIndex = adjacentActiveVideoIndex(current.videoIndex, -1);
  const nextVideoIndex = adjacentActiveVideoIndex(current.videoIndex, 1);
  document.getElementById("prevVideo").disabled = prevVideoIndex < 0;
  document.getElementById("nextVideo").disabled =
    nextVideoIndex < 0 || nextVideoIndex >= videos.length || nextVideoIndex > unlockedIndex;
  document.getElementById("prevDimension").disabled = current.dimensionIndex <= 0;
  document.getElementById("nextDimension").disabled = current.dimensionIndex >= (state.flow.dimensions || []).length - 1;
  document.getElementById("prevQuestion").disabled = current.questionIndex <= 0;
  document.getElementById("nextQuestion").disabled = current.questionIndex >= (current.dimension.questions || []).length - 1;
  document.getElementById("prevItem").disabled = adjacentActiveQuestionIndex(state.currentIndex, -1) < 0;
  document.getElementById("nextItem").disabled = adjacentActiveQuestionIndex(state.currentIndex, 1) < 0;
}

function renderAnswerForm(current) {
  const form = document.getElementById("answerForm");
  if (isCancelledItem(current)) {
    form.innerHTML = `
      <label>
        Evaluation Notes
        <textarea id="cancelledCaseInput" rows="5" readonly>This video has been cancelled</textarea>
      </label>
      <p class="field-helper">This video does not require evaluation. Its answers are not saved or included in statistics.</p>
    `;
    return;
  }
  const key = current.answerKey;
  const answer = state.answers[key] || {};
  const scoreConfig = current.question.score || state.flow.responseConfig?.score || { min: 0, max: 5, step: 1 };
  const confidenceConfig = current.question.confidence || state.flow.responseConfig?.confidence || { min: 1, max: 5, step: 1 };
  const explanationRequired = current.question.explanationRequired ?? state.flow.responseConfig?.explanationRequired;
  const scoreValue = answer.score ?? "";
  const confidenceValue = answer.confidence ?? "";
  const references = answerReferences(answer);
  const explanationBody = answerExplanationBody(answer);
  const review = pendingAnswerReview(current.answerKey);
  const reviewChanged = review && !answerValuesEqual(answer, review.marked_answer);

  form.innerHTML = `
    ${
      review
        ? `<div class="answer-review-card participant-review" role="status">
            <strong>${reviewChanged ? "This answer was updated and will return to complete after saving" : "An administrator requested a revision"}</strong>
            <p>${escapeHtml(review.comment || "Review and update this answer.")}</p>
          </div>`
        : ""
    }
    ${choiceControlHtml({
      inputId: "scoreInput",
      label: "Score",
      required: true,
      config: scoreConfig,
      value: scoreValue,
      helper: "Select a score using the current criterion's 0-5 rubric.",
    })}
    ${choiceControlHtml({
      inputId: "confidenceInput",
      label: "Confidence",
      required: true,
      config: confidenceConfig,
      value: confidenceValue,
      helper: "Select how confident you are in this rating.",
    })}
    <div class="explanation-field">
      <label id="explanationInputLabel">Evaluation Notes${explanationRequired ? " *" : ""}</label>
      <div
        id="explanationInput"
        class="explanation-editor"
        contenteditable="true"
        role="textbox"
        aria-multiline="true"
        aria-labelledby="explanationInputLabel"
        data-answer-key="${escapeHtml(current.answerKey)}"
        data-placeholder="Describe the evidence, observed issues, and relevant moments supporting the rating."
        spellcheck="true"
      ></div>
    </div>
  `;

  bindChoiceControl("scoreInput");
  bindChoiceControl("confidenceInput");
  const explanationEditor = document.getElementById("explanationInput");
  renderExplanationEditor(explanationEditor, answer);
  bindExplanationEditor(current, explanationEditor);
}

function activateAnswerReference(referenceId) {
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  const reference = answerReferences(state.answers[current.answerKey]).find(
    (item) => item.id === referenceId,
  );
  if (!reference) return;
  hideTextReferenceMenu();
  state.videoText.activeReferenceId = reference.id;
  if (isVideoTimeReference(reference)) {
    seekVideoToTime(
      document.querySelector("#videoContainer video"),
      Number(reference.time_seconds),
    );
    setVideoTextContent(state.videoText.cache[state.videoText.currentPath]?.text || "");
  } else if (reference.language && reference.language !== state.videoText.language) {
    state.videoText.language = reference.language;
    renderVideoText(current);
    renderVideoTextMode(current);
  } else {
    setVideoTextContent(state.videoText.cache[state.videoText.currentPath]?.text || "");
  }
  updateExplanationReferenceActiveState();
}

function removeAnswerReference(current, referenceId) {
  syncCurrentAnswer();
  const previous = state.answers[current.answerKey] || {};
  const references = answerReferences(previous);
  const nextReferences = references.filter((reference) => reference.id !== referenceId);
  if (nextReferences.length === references.length) return;
  const body = answerExplanationBody(previous);
  const referencePlacements = answerReferencePlacements(previous, references, body)
    .filter((placement) => placement.reference_id !== referenceId);
  const nextAnswer = {
    ...previous,
    explanation_body: body,
    references: nextReferences,
    reference_placements: referencePlacements,
    explanation: composeAnswerExplanation(nextReferences, body, referencePlacements),
  };
  if (
    String(nextAnswer.score ?? "") === ""
    && String(nextAnswer.confidence ?? "") === ""
    && !String(nextAnswer.explanation || "").trim()
  ) {
    delete state.answers[current.answerKey];
    markCurrentAnswerDirty(current.answerKey, previous, null);
  } else {
    state.answers[current.answerKey] = nextAnswer;
    markCurrentAnswerDirty(current.answerKey, previous, nextAnswer);
  }
  if (state.videoText.activeReferenceId === referenceId) {
    state.videoText.activeReferenceId = "";
  }
  renderAnswerForm(current);
  setVideoTextContent(state.videoText.cache[state.videoText.currentPath]?.text || "");
}

function choiceControlHtml({ inputId, label, required, config, value, helper }) {
  const min = Number(config.min ?? 0);
  const max = Number(config.max ?? 5);
  const step = Number(config.step || 1);
  const options = [];
  for (let option = min; option <= max; option += step) {
    options.push(String(option));
  }
  return `
    <fieldset class="choice-field">
      <legend>${escapeHtml(label)}${required ? " *" : ""}</legend>
      <input id="${escapeHtml(inputId)}" type="hidden" value="${escapeHtml(value)}">
      <div class="choice-row" role="radiogroup" aria-label="${escapeHtml(label)}">
        ${options
          .map(
            (option) => `
              <button class="choice-button${String(value) === option ? " is-selected" : ""}" type="button" data-choice-for="${escapeHtml(inputId)}" data-value="${escapeHtml(option)}" aria-pressed="${String(value) === option ? "true" : "false"}">
                ${escapeHtml(option)}
              </button>
            `,
          )
          .join("")}
      </div>
      <div class="choice-meta">
        <span>${escapeHtml(config.minLabel || String(min))}</span>
        <output id="${escapeHtml(inputId)}Output">${value === "" ? "Not selected" : escapeHtml(value)}</output>
        <span>${escapeHtml(config.maxLabel || String(max))}</span>
      </div>
      ${helper ? `<p class="field-helper">${escapeHtml(helper)}</p>` : ""}
    </fieldset>
  `;
}

function bindChoiceControl(inputId) {
  document.querySelectorAll(`[data-choice-for="${inputId}"]`).forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(inputId);
      const output = document.getElementById(`${inputId}Output`);
      input.value = button.dataset.value || "";
      if (output) output.textContent = input.value || "Not selected";
      document.querySelectorAll(`[data-choice-for="${inputId}"]`).forEach((item) => {
        const selected = item === button;
        item.classList.toggle("is-selected", selected);
        item.setAttribute("aria-pressed", selected ? "true" : "false");
      });
      syncCurrentAnswer();
    });
  });
}

function syncCurrentAnswer() {
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  if (isCancelledItem(current)) return;
  const scoreInput = document.getElementById("scoreInput");
  const confidenceInput = document.getElementById("confidenceInput");
  const explanationInput = document.getElementById("explanationInput");
  if (!scoreInput || !confidenceInput || !explanationInput) return;
  const score = scoreInput.value;
  const confidence = confidenceInput.value;
  const scoreOutput = document.getElementById("scoreInputOutput");
  const confidenceOutput = document.getElementById("confidenceInputOutput");
  if (scoreOutput) scoreOutput.textContent = score || "Not selected";
  if (confidenceOutput) confidenceOutput.textContent = confidence || "Not selected";
  const previousAnswer = state.answers[current.answerKey];
  const previousReferences = answerReferences(previousAnswer);
  const caretBodyOffset = explanationEditorCaretBodyOffset(
    explanationInput,
    previousReferences,
  );
  const editorValue = readExplanationEditor(explanationInput, previousReferences);
  const mergedEditorValue = mergeExplanationEditorPlacements(
    previousAnswer,
    previousReferences,
    editorValue,
  );
  const references = previousReferences;
  const explanationBody = editorValue.body;
  const referencePlacements = mergedEditorValue.placements;
  const explanation = composeAnswerExplanation(
    references,
    explanationBody,
    referencePlacements,
  );
  const usesStructuredReferences =
    references.length > 0
    || Object.prototype.hasOwnProperty.call(previousAnswer || {}, "references")
    || Object.prototype.hasOwnProperty.call(previousAnswer || {}, "explanation_body")
    || Object.prototype.hasOwnProperty.call(previousAnswer || {}, "reference_placements");
  let nextAnswer = null;
  if (score === "" && confidence === "" && !String(explanation || "").trim()) {
    delete state.answers[current.answerKey];
  } else {
    nextAnswer = { score, confidence, explanation };
    if (usesStructuredReferences) {
      nextAnswer.explanation_body = explanationBody;
      nextAnswer.references = references;
      nextAnswer.reference_placements = referencePlacements;
    }
    state.answers[current.answerKey] = nextAnswer;
  }
  if (nextAnswer && mergedEditorValue.missingReferenceIds.length > 0) {
    const restoreFocus = document.activeElement === explanationInput;
    renderExplanationEditor(explanationInput, nextAnswer);
    const restoredRange = explanationEditorRangeAtBodyOffset(
      explanationInput,
      caretBodyOffset ?? textCharacterCount(explanationBody),
    );
    setExplanationEditorRange(
      current,
      explanationInput,
      restoredRange,
      restoreFocus,
    );
  }
  markCurrentAnswerDirty(current.answerKey, previousAnswer, nextAnswer);
}

function markCurrentAnswerDirty(answerKey, previousAnswer, nextAnswer) {
  if (!answerValuesEqual(previousAnswer, nextAnswer)) {
    state.dirtyAnswerKeys.add(answerKey);
  }
  saveDraft();
  scheduleProgressSave();
  updateCurrentSelectorStatus();
}

function moveQuestion(delta) {
  if (!evaluationNavigationAllowed()) return;
  syncCurrentAnswer();
  const nextIndex = adjacentActiveQuestionIndex(state.currentIndex, delta);
  if (nextIndex < 0 || nextIndex >= state.flatQuestions.length) return;
  const current = state.flatQuestions[state.currentIndex];
  const next = state.flatQuestions[nextIndex];
  if (delta > 0 && next.videoIndex !== current.videoIndex && !isVideoComplete(current.videoIndex)) {
    showAlert("Complete every criterion for the current video before continuing.");
    updateCurrentSelectorStatus();
    return;
  }
  state.currentIndex = nextIndex;
  renderEvaluation();
}

function moveVideo(delta) {
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  const nextIndex = adjacentActiveVideoIndex(current.videoIndex, delta);
  if (nextIndex < 0) return;
  selectVideo(nextIndex);
}

function moveDimension(delta) {
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  selectDimension(current.dimensionIndex + delta);
}

function moveQuestionWithinDimension(delta) {
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  selectQuestion(current.questionIndex + delta);
}

function selectVideo(videoIndex) {
  const current = state.flatQuestions[state.currentIndex];
  if (!current || videoIndex < 0 || videoIndex >= participantVideos().length) return;
  if (!canAccessVideo(videoIndex)) {
    showAlert("This video is locked. Complete all earlier videos first.");
    return;
  }
  goToSelection(videoIndex, current.dimensionIndex, current.questionIndex);
}

function selectDimension(dimensionIndex) {
  const current = state.flatQuestions[state.currentIndex];
  if (!current || dimensionIndex < 0 || dimensionIndex >= (state.flow.dimensions || []).length) return;
  goToSelection(current.videoIndex, dimensionIndex, 0);
}

function selectQuestion(questionIndex) {
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  const questions = current.dimension.questions || [];
  if (questionIndex < 0 || questionIndex >= questions.length) return;
  goToSelection(current.videoIndex, current.dimensionIndex, questionIndex);
}

function goToSelection(videoIndex, dimensionIndex, questionIndex) {
  if (!evaluationNavigationAllowed()) return;
  syncCurrentAnswer();
  if (!canAccessVideo(videoIndex)) {
    showAlert("This video is locked. Complete all earlier videos first.");
    return;
  }
  const target = state.flatQuestions.findIndex(
    (item) =>
      item.videoIndex === videoIndex &&
      item.dimensionIndex === dimensionIndex &&
      item.questionIndex === questionIndex,
  );
  if (target < 0) return;
  state.currentIndex = target;
  renderEvaluation();
}

function maxUnlockedVideoIndex() {
  const videos = participantVideos();
  if (!videos.length) return -1;
  const firstIncomplete = videos.findIndex((_, videoIndex) => !isVideoComplete(videoIndex));
  return firstIncomplete < 0 ? videos.length - 1 : firstIncomplete;
}

function adjacentActiveVideoIndex(startIndex, delta) {
  const videos = participantVideos();
  for (let index = startIndex + delta; index >= 0 && index < videos.length; index += delta) {
    if (!isCancelledVideo(videos[index])) return index;
  }
  return -1;
}

function adjacentActiveQuestionIndex(startIndex, delta) {
  for (let index = startIndex + delta; index >= 0 && index < state.flatQuestions.length; index += delta) {
    if (!isCancelledItem(state.flatQuestions[index])) return index;
  }
  return -1;
}

function canAccessVideo(videoIndex) {
  return videoIndex >= 0 && videoIndex <= maxUnlockedVideoIndex();
}

async function submitEvaluation() {
  syncCurrentAnswer();
  sanitizeCancelledAnswers();
  if (!state.participantLoggedIn) {
    showAlert("Participant sign-in is required.");
    showView("participant");
    return;
  }
  const participant = readParticipantForm();
  if (!participant) return;
  await flushProgressSave();
  const missing = findMissingAnswers();
  if (missing.length) {
    showAlert(`Incomplete items remain: ${missing.slice(0, 6).join(", ")}`);
    return;
  }
  const response = await apiPost("/api/submissions", {
    flow_id: state.flow.id,
    participant: state.participant,
    answers: state.answers,
    changed_answer_keys: [],
  });
  state.currentSubmission = response.submission;
  state.answerReviews = response.submission?.answer_reviews || {};
  state.videoOrder = response.submission?.video_order || state.videoOrder;
  state.participantKey = response.participant_key || response.submission?.participant_key || state.participantKey;
  applyUsagePolicy(response.usage);
  saveDraft();
  renderEvaluation();
  showAlert(`Submission complete. Record ID: ${response.submission.id}. Sign in with the same participant identifier to review or revise it.`, false);
}

function findMissingAnswers() {
  const missing = [];
  for (const item of state.flatQuestions) {
    if (isCancelledItem(item)) continue;
    const answer = state.answers[item.answerKey];
    const explanationRequired = item.question.explanationRequired ?? state.flow.responseConfig?.explanationRequired;
    if (isAnswerPendingRevision(item.answerKey)) {
      missing.push(`Video ${item.videoIndex + 1}/${item.dimension.title}/${item.question.prompt}/revision requested`);
      continue;
    }
    if (!answer || answer.score === "" || answer.confidence === "") {
      missing.push(`Video ${item.videoIndex + 1}/${item.dimension.title}/${item.question.prompt}`);
      continue;
    }
    if (explanationRequired && !String(answer.explanation || "").trim()) {
      missing.push(`Video ${item.videoIndex + 1}/${item.dimension.title}/${item.question.prompt}/notes`);
    }
  }
  return missing;
}

function isVideoComplete(videoIndex) {
  if (isCancelledVideo(participantVideos()[videoIndex])) return true;
  const items = state.flatQuestions.filter((item) => item.videoIndex === videoIndex);
  return items.length > 0 && items.every(isAnswerComplete);
}

function isDimensionComplete(videoIndex, dimensionIndex) {
  if (isCancelledVideo(participantVideos()[videoIndex])) return true;
  const items = state.flatQuestions.filter(
    (item) => item.videoIndex === videoIndex && item.dimensionIndex === dimensionIndex,
  );
  return items.length > 0 && items.every(isAnswerComplete);
}

function isQuestionComplete(videoIndex, dimensionIndex, questionIndex) {
  if (isCancelledVideo(participantVideos()[videoIndex])) return true;
  const item = state.flatQuestions.find(
    (entry) =>
      entry.videoIndex === videoIndex &&
      entry.dimensionIndex === dimensionIndex &&
      entry.questionIndex === questionIndex,
  );
  return item ? isAnswerComplete(item) : false;
}

function isAnswerComplete(item) {
  if (isCancelledItem(item)) return true;
  const answer = state.answers[item.answerKey];
  const explanationRequired = item.question.explanationRequired ?? state.flow.responseConfig?.explanationRequired;
  if (!answer || answer.score === "" || answer.score == null) return false;
  if (answer.confidence === "" || answer.confidence == null) return false;
  if (explanationRequired && !String(answer.explanation || "").trim()) return false;
  if (isAnswerPendingRevision(item.answerKey)) return false;
  return true;
}

function pendingAnswerReview(answerKey) {
  const review = state.answerReviews?.[answerKey];
  return review && review.status === "needs_revision" ? review : null;
}

function isAnswerPendingRevision(answerKey) {
  const review = pendingAnswerReview(answerKey);
  if (!review) return false;
  return answerValuesEqual(state.answers[answerKey], review.marked_answer);
}

function answerValuesEqual(left, right) {
  const normalize = (value) => {
    if (!value || typeof value !== "object") return null;
    const normalized = {
      score: String(value.score ?? ""),
      confidence: String(value.confidence ?? ""),
      explanation: String(value.explanation ?? ""),
    };
    const hasStructuredReferences =
      Array.isArray(value.references)
      || Object.prototype.hasOwnProperty.call(value, "explanation_body");
    if (hasStructuredReferences) {
      normalized.explanation_body = String(value.explanation_body ?? "");
      normalized.references = answerReferences(value).map((reference) => {
        if (isVideoTimeReference(reference)) {
          return {
            id: String(reference.id || ""),
            type: "video_time",
            video_id: String(reference.video_id || ""),
            time_seconds: Number(reference.time_seconds),
          };
        }
        return {
          id: String(reference.id || ""),
          type: "text",
          video_id: String(reference.video_id || ""),
          language: String(reference.language || ""),
          source_key: String(reference.source_key || ""),
          start: Number(reference.start),
          end: Number(reference.end),
          source_length: Number(reference.source_length),
          text: String(reference.text || ""),
          prefix: String(reference.prefix || ""),
          suffix: String(reference.suffix || ""),
        };
      });
      normalized.reference_placements = answerReferencePlacements(
        value,
        answerReferences(value),
        answerExplanationBody(value),
      );
      normalized.explanation = composeAnswerExplanation(
        normalized.references,
        normalized.explanation_body,
        normalized.reference_placements,
      );
    }
    return normalized;
  };
  return JSON.stringify(normalize(left)) === JSON.stringify(normalize(right));
}

function updateCurrentSelectorStatus() {
  const current = state.flatQuestions[state.currentIndex];
  if (!current) return;
  const videoSelect = document.getElementById("videoSelectButton");
  const dimensionSelect = document.getElementById("dimensionSelectButton");
  const questionSelect = document.getElementById("questionSelectButton");
  if (!videoSelect || !dimensionSelect || !questionSelect) return;
  renderVideoSelect(current);
  renderDimensionSelect(current);
  renderQuestionSelect(current);
}

function firstIncompleteIndex() {
  state.flatQuestions = flattenQuestions(state.flow, state.videoOrder);
  const index = state.flatQuestions.findIndex((item) => !isAnswerComplete(item));
  if (index >= 0) return index;
  const firstActive = state.flatQuestions.findIndex((item) => !isCancelledItem(item));
  return firstActive >= 0 ? firstActive : 0;
}

function updateNavigationState() {
  document.querySelectorAll('.tab[data-view="instructions"], .tab[data-view="evaluation"]').forEach((button) => {
    button.disabled = !state.participantLoggedIn;
  });
  document.getElementById("logoutParticipant").hidden = !state.participantLoggedIn;
}

function applyUsagePolicy(usage) {
  if (!usage || typeof usage !== "object") return;
  const wasBlocked = usagePolicyBlocksEvaluation();
  state.usagePolicy = usage;
  renderUsagePolicyBanner();
  if (
    wasBlocked
    && !usagePolicyBlocksEvaluation()
    && !document.getElementById("evaluationView").hidden
  ) {
    renderEvaluation();
    return;
  }
  applyEvaluationPolicyControls();
}

function usagePolicyBlocksEvaluation() {
  return !state.adminUnlocked && Boolean(state.usagePolicy?.blocked);
}

function evaluationNavigationAllowed() {
  if (!usagePolicyBlocksEvaluation()) return true;
  showAlert(
    state.usagePolicy?.messages?.join(" ")
      || "Evaluation access is paused for today. Current input will still be saved.",
  );
  return false;
}

function applyEvaluationPolicyControls() {
  const blocked = usagePolicyBlocksEvaluation();
  const controlIds = [
    "prevItem",
    "nextItem",
    "prevVideo",
    "nextVideo",
    "prevDimension",
    "nextDimension",
    "prevQuestion",
    "nextQuestion",
    "videoSelectButton",
    "dimensionSelectButton",
    "questionSelectButton",
  ];
  if (blocked) {
    controlIds.forEach((id) => {
      const control = document.getElementById(id);
      if (control) control.disabled = true;
    });
    document.getElementById("videoContainer")?.querySelector("video")?.pause();
  }
}

function renderUsagePolicyBanner() {
  const banner = document.getElementById("usagePolicyBanner");
  if (!banner) return;
  const usage = state.usagePolicy;
  const restricted = Boolean(
    usage
    && (
      usage.blocked
      || Number(usage.traffic_tier) > 0
      || Number(usage.refresh_tier) > 0
    ),
  );
  banner.hidden = !restricted;
  banner.classList.toggle("is-blocked", Boolean(usage?.blocked));
  if (!restricted) return;
  document.getElementById("usagePolicyTitle").textContent = usage.blocked
    ? "Evaluation Access Paused Today"
    : "Usage Rate Adjusted";
  document.getElementById("usagePolicyMessage").textContent =
    (usage.messages || []).join(" ") || "A usage limit is active.";
  document.getElementById("usagePolicyTraffic").textContent =
    `${Number(usage.egress_gb || 0).toFixed(3)} GB`;
  document.getElementById("usagePolicyReloads").textContent =
    String(usage.reload_count || 0);
  document.getElementById("usagePolicyRate").textContent =
    usageFactorLabel(Number(usage.effective_factor ?? 1));
  document.getElementById("usagePolicyReset").textContent =
    formatPolicyResetTime(usage.reset_at);
}

function usageFactorLabel(value) {
  if (Math.abs(value - 1) < 1e-8) return "Normal";
  if (Math.abs(value) < 1e-8) return "Paused";
  for (let denominator = 2; denominator <= 16; denominator += 1) {
    const numerator = Math.round(value * denominator);
    if (numerator > 0 && Math.abs(value - numerator / denominator) < 1e-8) {
      return `${numerator}/${denominator} normal`;
    }
  }
  return `${value.toFixed(3)} times normal`;
}

function formatPolicyResetTime(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return "Next day at 00:00";
  return date.toLocaleString("en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

async function refreshUsagePolicy() {
  if (!state.participantLoggedIn || document.getElementById("evaluationView").hidden) return;
  const response = await apiGet("/api/usage/me");
  applyUsagePolicy(response.usage);
}

function startUsagePolicyPolling() {
  if (
    state.usagePollTimer
    || !state.participantLoggedIn
    || document.hidden
    || document.getElementById("evaluationView").hidden
  ) {
    return;
  }
  refreshUsagePolicy().catch(() => {});
  state.usagePollTimer = setInterval(() => {
    refreshUsagePolicy().catch(() => {});
  }, 60_000);
}

function stopUsagePolicyPolling() {
  if (!state.usagePollTimer) return;
  clearInterval(state.usagePollTimer);
  state.usagePollTimer = null;
}

function scheduleProgressSave() {
  if (!state.participantLoggedIn || !state.flow || !state.participantKey) return;
  clearTimeout(state.autosaveTimer);
  state.autosaveTimer = setTimeout(() => {
    saveProgressToServer().catch((error) => showAlert(error.message || String(error)));
  }, 700);
}

async function flushProgressSave() {
  if (state.autosaveTimer) {
    clearTimeout(state.autosaveTimer);
    state.autosaveTimer = null;
    await saveProgressToServer();
  }
  if (state.saveInFlight) await state.saveInFlight;
}

async function saveProgressToServer() {
  if (!state.participantLoggedIn || !state.flow || !state.participantKey) return;
  state.autosaveTimer = null;
  const previousSave = state.saveInFlight || Promise.resolve();
  const saveTask = previousSave.catch(() => {}).then(performProgressSave);
  state.saveInFlight = saveTask;
  try {
    return await saveTask;
  } finally {
    if (state.saveInFlight === saveTask) state.saveInFlight = null;
  }
}

async function performProgressSave() {
  sanitizeCancelledAnswers();
  const changedKeys = [...state.dirtyAnswerKeys];
  if (!changedKeys.length) return state.currentSubmission;
  const sentAnswers = Object.fromEntries(
    changedKeys
      .filter((key) => Object.prototype.hasOwnProperty.call(state.answers, key))
      .map((key) => [key, { ...state.answers[key] }]),
  );
  const response = await apiPost("/api/submissions/draft", {
    flow_id: state.flow.id,
    participant: state.participant,
    answers: sentAnswers,
    changed_answer_keys: changedKeys,
  });
  for (const key of changedKeys) {
    const sentAnswer = sentAnswers[key] || null;
    const currentAnswer = state.answers[key] || null;
    if (answerValuesEqual(currentAnswer, sentAnswer)) state.dirtyAnswerKeys.delete(key);
  }
  state.currentSubmission = response.submission;
  state.answerReviews = response.submission?.answer_reviews || state.answerReviews;
  state.videoOrder = response.submission?.video_order || state.videoOrder;
  state.participantKey = response.participant_key || response.submission?.participant_key || state.participantKey;
  applyUsagePolicy(response.usage);
  updateCurrentSelectorStatus();
  return response.submission;
}

function persistProgressOnExit() {
  if (!state.participantLoggedIn || !state.flow || !state.participantKey) return;
  syncCurrentAnswer();
  sanitizeCancelledAnswers();
  saveDraft();
  clearTimeout(state.autosaveTimer);
  state.autosaveTimer = null;
  // Avoid racing an older save request against the unload beacon for the same answer.
  // The latest local draft remains available for recovery on the next sign-in.
  if (state.saveInFlight || !state.dirtyAnswerKeys.size) return;
  const payload = JSON.stringify({
    flow_id: state.flow.id,
    participant: state.participant,
    answers: Object.fromEntries(
      [...state.dirtyAnswerKeys]
        .filter((key) => Object.prototype.hasOwnProperty.call(state.answers, key))
        .map((key) => [key, state.answers[key]]),
    ),
    changed_answer_keys: [...state.dirtyAnswerKeys],
  });
  if (navigator.sendBeacon) {
    navigator.sendBeacon("/api/submissions/draft", new Blob([payload], { type: "application/json" }));
    return;
  }
  fetch("/api/submissions/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => {});
}

async function ensureAdminAccess() {
  if (state.adminUnlocked && sessionStorage.getItem(ADMIN_PASSWORD_STORAGE_KEY)) return true;
  let password = sessionStorage.getItem(ADMIN_PASSWORD_STORAGE_KEY);
  if (!password) {
    password = window.prompt("Enter the administrator password");
  }
  if (!password) return false;
  sessionStorage.setItem(ADMIN_PASSWORD_STORAGE_KEY, password);
  try {
    await apiGet("/api/admin/check", { admin: true });
    state.adminUnlocked = true;
    return true;
  } catch (error) {
    state.adminUnlocked = false;
    sessionStorage.removeItem(ADMIN_PASSWORD_STORAGE_KEY);
    showAlert(error.message || "Incorrect administrator password.");
    return false;
  }
}

async function loadAdminFlows() {
  const response = await apiGet("/api/flows?include_drafts=1", { admin: true });
  state.flows = response.flows || [];
  if (!state.flows.find((flow) => flow.id === state.flow?.id) && state.flows.length) {
    state.flow = state.flows[0];
    state.flatQuestions = flattenQuestions(state.flow);
  }
  renderAdmin();
}

function renderAdmin() {
  const select = document.getElementById("flowSelect");
  select.innerHTML = "";
  for (const flow of state.flows) {
    const option = document.createElement("option");
    option.value = flow.id;
    option.textContent = `${flow.title} (${flowStatusLabel(flow.status)})`;
    option.selected = flow.id === state.flow.id;
    select.append(option);
  }
  document.getElementById("flowEditor").value = JSON.stringify(state.flow, null, 2);
  document.getElementById("flowStatusBadge").textContent = `${flowStatusLabel(state.flow.status)} | Version ${state.flow.version || 1}`;
}

async function loadAdminTraffic() {
  if (!state.adminUnlocked) return;
  const response = await apiGet("/api/admin/traffic/daily", { admin: true });
  renderAdminTraffic(response.usage || [], response.alerts || [], response.allowlist || {});
}

function renderAdminTraffic(usageRows, alerts, allowlist) {
  const allowlistStatus = document.getElementById("adminAllowlistStatus");
  const hash = String(allowlist.active_hash || "").slice(0, 12);
  allowlistStatus.textContent = allowlist.healthy
    ? `Allowlist healthy | ${allowlist.entry_count || 0} identifiers | Version ${hash || "unknown"} | ${formatDateTime(allowlist.last_loaded_at)}`
    : `Allowlist error: ${allowlist.last_error || "no valid allowlist loaded"}`;

  const table = document.getElementById("adminTrafficTable");
  table.innerHTML = "";
  if (!usageRows.length) {
    const row = document.createElement("tr");
    row.innerHTML = `<td colspan="7" class="muted">No participant traffic has been recorded today.</td>`;
    table.append(row);
  } else {
    usageRows.forEach((item) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${escapeHtml(item.participant_name || item.participant_key || "Unknown identifier")}</td>
        <td>${(Number(item.egress_bytes || 0) / 1_000_000_000).toFixed(3)} GB</td>
        <td>${Number(item.reported_reload_count || 0)}</td>
        <td>${escapeHtml(usageFactorLabel(Number(item.effective_factor ?? 1)))}</td>
        <td>${Number(item.video_request_count || 0)}</td>
        <td>${Number(item.rejected_request_count || 0)}</td>
        <td>${escapeHtml(formatDateTime(item.last_seen_at))}</td>
      `;
      table.append(row);
    });
  }

  const unacknowledged = alerts.filter((item) => !item.acknowledged_at);
  document.getElementById("adminAlertCount").textContent = String(unacknowledged.length);
  const list = document.getElementById("adminTrafficAlerts");
  list.innerHTML = "";
  if (!alerts.length) {
    const empty = document.createElement("div");
    empty.className = "traffic-alert-empty";
    empty.textContent = "No traffic or reload thresholds have been triggered today.";
    list.append(empty);
    return;
  }
  alerts.forEach((item) => {
    const alert = document.createElement("article");
    alert.className = `traffic-alert-item${item.acknowledged_at ? " is-acknowledged" : ""}`;
    const type = item.alert_type === "refresh" ? "Reload Alert" : "Traffic Alert";
    alert.innerHTML = `
      <div>
        <strong>${escapeHtml(item.participant_name || item.participant_key || "Unknown identifier")}</strong>
        <p class="muted">${escapeHtml(type)} | ${escapeHtml(formatDateTime(item.created_at))}</p>
      </div>
      <div>${escapeHtml(item.message || "")}</div>
    `;
    if (!item.acknowledged_at) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "Acknowledge";
      button.addEventListener("click", async () => {
        await apiPost(
          `/api/admin/traffic/alerts/${encodeURIComponent(item.id)}/acknowledge`,
          {},
          { admin: true },
        );
        await loadAdminTraffic();
      });
      alert.append(button);
    } else {
      const acknowledged = document.createElement("span");
      acknowledged.className = "muted";
      acknowledged.textContent = "Acknowledged";
      alert.append(acknowledged);
    }
    list.append(alert);
  });
}

async function loadSelectedFlow() {
  const id = document.getElementById("flowSelect").value;
  const response = await apiGet(`/api/flows/${encodeURIComponent(id)}`, { admin: true });
  state.flow = response.flow;
  state.flatQuestions = flattenQuestions(state.flow);
  renderFlowSummary();
  renderParticipantForm();
  renderInstructions();
  renderAdmin();
  showAlert("Workflow reloaded.", false);
}

async function saveFlow(status) {
  if (!(await ensureAdminAccess())) return;
  const flow = parseFlowEditor();
  if (!flow) return;
  flow.status = status;
  const response = await apiPost("/api/flows", flow, { admin: true });
  state.flow = response.flow;
  await loadAdminFlows();
  showView("admin");
  showAlert(status === "draft" ? "Draft saved." : "Workflow saved.", false);
}

async function publishFlow() {
  if (!(await ensureAdminAccess())) return;
  const flow = parseFlowEditor();
  if (!flow) return;
  await apiPost("/api/flows", { ...flow, status: "draft" }, { admin: true });
  const response = await apiPost(`/api/flows/${encodeURIComponent(flow.id)}/publish`, {}, { admin: true });
  state.flow = response.flow;
  await loadAdminFlows();
  showView("admin");
  showAlert("Workflow published.", false);
}

function parseFlowEditor() {
  try {
    return JSON.parse(document.getElementById("flowEditor").value);
  } catch (error) {
    showAlert(`Workflow JSON could not be parsed: ${error.message}`);
    return null;
  }
}

async function renderResults() {
  if (!(await ensureAdminAccess())) return;
  const table = document.getElementById("resultsTable");
  const query = state.flow ? `?flow_id=${encodeURIComponent(state.flow.id)}` : "";
  const selectedSubmissionId = currentResult()?.id || null;
  const response = await apiGet(`/api/submissions${query}`, { admin: true });
  state.results = response.submissions || [];
  table.innerHTML = "";

  if (!state.results.length) {
    table.innerHTML = '<tr><td colspan="7" class="empty-cell">This workflow has no evaluation records.</td></tr>';
    closeResultDetail();
    return;
  }

  state.results.forEach((item, index) => {
    const stats = calculateSubmissionStats(item, state.flow);
    const participant = participantName(item.participant, state.flow);
    const pinnedBadge = item.is_pinned ? '<span class="result-pin-badge">Pinned</span>' : "";
    const pinLabel = item.is_pinned ? "Unpin" : "Pin";
    const pendingCount = pendingReviewCount(item);
    const row = document.createElement("tr");
    row.className = item.is_pinned ? "result-row is-pinned" : "result-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", `View evaluation results for ${participant}`);
    row.title = "View result details";
    row.innerHTML = `
      <td><span class="result-participant-cell">${pinnedBadge}<span>${escapeHtml(participant)}</span></span></td>
      <td>${escapeHtml(statusLabel(item.status))}${pendingCount ? `<br><span class="review-count">${pendingCount} revisions</span>` : ""}</td>
      <td>${escapeHtml(formatDateTime(item.updated_at || item.created_at))}</td>
      <td>${Object.keys(item.answers || {}).length}</td>
      <td>${escapeHtml(formatScore(stats.totalAverage))}</td>
      <td>${escapeHtml(formatScore(stats.totalScore))}</td>
      <td>
        <div class="result-actions">
          <button class="link-button result-action" type="button" data-result-action="pin" data-result-index="${index}">${escapeHtml(pinLabel)}</button>
          <button class="link-button result-action danger-action" type="button" data-result-action="hide" data-result-index="${index}">Hide</button>
          <button class="link-button result-action" type="button" data-result-action="view" data-result-index="${index}">View</button>
        </div>
      </td>
    `;
    row.addEventListener("click", () => openResultDetail(index));
    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openResultDetail(index);
    });
    row.querySelectorAll("[data-result-action]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const action = button.dataset.resultAction;
        if (action === "view") {
          openResultDetail(index);
          return;
        }
        const task = action === "pin" ? toggleResultPinned(index) : hideResult(index);
        task.catch((error) => showAlert(error.message || String(error)));
      });
    });
    table.append(row);
  });

  if (selectedSubmissionId) {
    const nextIndex = state.results.findIndex((item) => item.id === selectedSubmissionId);
    if (nextIndex < 0) {
      closeResultDetail();
    } else {
      state.selectedResultIndex = nextIndex;
      renderResultDetail();
    }
  } else if (state.selectedResultIndex >= state.results.length) {
    closeResultDetail();
  } else if (state.selectedResultIndex >= 0) {
    renderResultDetail();
  }
}

async function toggleResultPinned(index) {
  const item = state.results[index];
  if (!item) return;
  const nextPinned = !item.is_pinned;
  await updateSubmissionAdminFlags(item.id, { is_pinned: nextPinned });
  await renderResults();
  showAlert(nextPinned ? "Result pinned." : "Result unpinned.", false);
}

async function hideResult(index) {
  const item = state.results[index];
  if (!item) return;
  const participant = participantName(item.participant, state.flow);
  const confirmed = window.confirm(
    `Hide the result for "${participant}"?\n\nThe database record will remain available in CSV exports.`,
  );
  if (!confirmed) return;
  if (currentResult()?.id === item.id) closeResultDetail();
  await updateSubmissionAdminFlags(item.id, { is_hidden: true });
  await renderResults();
  showAlert("The result is hidden from this view. The database record was not deleted.", false);
}

async function updateSubmissionAdminFlags(submissionId, flags) {
  if (!submissionId) throw new Error("Submission ID is missing.");
  return apiPost(`/api/submissions/${encodeURIComponent(submissionId)}/admin-flags`, flags, { admin: true });
}

function openResultDetail(index) {
  if (index < 0 || index >= state.results.length) return;
  state.selectedResultIndex = index;
  state.resultView.videoIndex = 0;
  state.resultView.dimensionIndex = 0;
  state.resultView.questionIndex = 0;
  state.resultView.mode = "detail";
  setResultCompletionExpanded(false);
  renderResultDetail();
  document.getElementById("resultDetail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeResultDetail() {
  state.selectedResultIndex = -1;
  document.getElementById("resultDetail").hidden = true;
}

function currentResult() {
  return state.results[state.selectedResultIndex] || null;
}

function toggleResultCompletionOverview() {
  const overview = document.getElementById("resultCompletionOverview");
  setResultCompletionExpanded(overview.hidden);
}

function setResultCompletionExpanded(expanded) {
  const overview = document.getElementById("resultCompletionOverview");
  const button = document.getElementById("toggleResultCompletion");
  overview.hidden = !expanded;
  button.textContent = expanded ? "Hide Completion" : "Show Completion";
  button.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function renderResultDetail() {
  const result = currentResult();
  const detail = document.getElementById("resultDetail");
  if (!result) {
    detail.hidden = true;
    return;
  }

  detail.hidden = false;
  clampResultView();
  const flow = state.flow || {};
  const videos = resultVideos(flow, result);
  const dimensions = flow.dimensions || [];
  const video = videos[state.resultView.videoIndex] || null;
  const dimension = dimensions[state.resultView.dimensionIndex] || null;
  const questions = dimension?.questions || [];
  const question = questions[state.resultView.questionIndex] || null;

  document.getElementById("resultDetailTitle").textContent = participantName(result.participant, flow);
  document.getElementById("resultDetailMeta").textContent = [
    `Status: ${statusLabel(result.status)}${pendingReviewCount(result) ? ` (${pendingReviewCount(result)} revisions)` : ""}`,
    `Updated: ${formatDateTime(result.updated_at || result.created_at)}`,
    `Workflow: ${flow.title || result.flow_id} v${result.flow_version || flow.version || 1}`,
    `Answers: ${Object.keys(result.answers || {}).length}`,
  ].join(" | ");

  renderResultCompletionOverview(result, flow);
  renderResultStats(result, flow);
  renderResultMode();
  if (state.resultView.mode === "detail") {
    renderResultSelectors(flow);
    renderResultVideo(video, flow);
    renderResultAnswer(result, video, dimension, question, flow);
    updateResultNavigationButtons(video, dimension, question);
  }
}

function renderResultSelectors(flow) {
  const result = currentResult();
  const videos = resultVideos(flow, currentResult());
  const dimensions = flow.dimensions || [];
  const video = videos[state.resultView.videoIndex] || null;
  const dimension = dimensions[state.resultView.dimensionIndex] || null;
  const questions = dimension?.questions || [];

  renderNativeSelect(
    document.getElementById("resultVideoSelect"),
    videos.map((item, index) => {
      const progress = resultScopeProgress(result, flow, item);
      return `${index + 1}/${videos.length} ${item.title} | ${resultProgressText(progress)}`;
    }),
    state.resultView.videoIndex,
  );
  renderNativeSelect(
    document.getElementById("resultDimensionSelect"),
    dimensions.map((item, index) => {
      const progress = resultScopeProgress(result, flow, video, item);
      return `Dimension ${index + 1}/${dimensions.length} ${item.title} | ${resultProgressText(progress)}`;
    }),
    state.resultView.dimensionIndex,
  );
  renderNativeSelect(
    document.getElementById("resultQuestionSelect"),
    questions.map((item, index) => {
      const status = resultAnswerCompletionStatus(result, flow, video, dimension, item);
      return `Criterion ${index + 1}/${questions.length} ${item.prompt} | ${resultAnswerStatusText(status)}`;
    }),
    state.resultView.questionIndex,
  );

  document.getElementById("resultQuestionProgress").textContent =
    dimensions.length && questions.length
      ? `Dimension ${state.resultView.dimensionIndex + 1}/${dimensions.length} | Criterion ${state.resultView.questionIndex + 1}/${questions.length}`
      : "Not configured";
}

function renderResultCompletionOverview(result, flow) {
  const videos = resultVideos(flow, result);
  const activeVideos = videos.filter((video) => !isCancelledVideo(video));
  const dimensions = flow.dimensions || [];
  const caseProgress = document.getElementById("resultCaseProgress");
  const subdimensionProgress = document.getElementById("resultSubdimensionProgress");
  caseProgress.innerHTML = "";
  subdimensionProgress.innerHTML = "";

  const overall = resultScopeProgress(result, flow);
  const completedCases = activeVideos.filter((video) => {
    const progress = resultScopeProgress(result, flow, video);
    return progress.total > 0 && progress.complete === progress.total;
  }).length;
  const startedCases = activeVideos.filter((video) => resultScopeProgress(result, flow, video).started > 0).length;
  const cancelledCases = videos.length - activeVideos.length;
  document.getElementById("resultCompletionSummary").textContent = [
    `Completed videos: ${completedCases}/${activeVideos.length}`,
    `Started: ${startedCases}/${activeVideos.length}`,
    cancelledCases ? `Cancelled: ${cancelledCases}` : "",
    `Completed criteria: ${overall.complete}/${overall.total}`,
    overall.needsRevision ? `Needs revision: ${overall.needsRevision}` : "Needs revision: 0",
  ].filter(Boolean).join(" | ");

  if (!videos.length) {
    caseProgress.innerHTML = '<p class="muted result-progress-empty">This workflow has no videos.</p>';
  }
  videos.forEach((video, videoIndex) => {
    const progress = resultScopeProgress(result, flow, video);
    const status = resultProgressStatus(progress);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `result-case-button status-${status}`;
    button.classList.toggle("is-selected", videoIndex === state.resultView.videoIndex);
    button.setAttribute("aria-pressed", videoIndex === state.resultView.videoIndex ? "true" : "false");
    button.setAttribute("aria-label", `${video.title}, ${resultProgressText(progress)}`);

    const title = document.createElement("span");
    title.className = "result-case-title";
    title.textContent = `${videoIndex + 1}. ${video.title}`;
    const progressText = document.createElement("span");
    progressText.className = "result-case-status";
    progressText.textContent = resultProgressText(progress);
    button.append(title, progressText);
    button.addEventListener("click", () => {
      state.resultView.videoIndex = videoIndex;
      state.resultView.mode = "detail";
      renderResultDetail();
    });
    caseProgress.append(button);
  });

  const video = videos[state.resultView.videoIndex] || null;
  if (!video || !dimensions.length) {
    subdimensionProgress.innerHTML = '<p class="muted result-progress-empty">The current video has no criteria.</p>';
    return;
  }

  dimensions.forEach((dimension, dimensionIndex) => {
    const progress = resultScopeProgress(result, flow, video, dimension);
    const card = document.createElement("section");
    card.className = `result-dimension-progress status-${resultProgressStatus(progress)}`;

    const heading = document.createElement("button");
    heading.type = "button";
    heading.className = "result-dimension-progress-heading";
    heading.innerHTML = `<span>${escapeHtml(dimension.title)}</span><strong>${escapeHtml(resultProgressText(progress))}</strong>`;
    heading.addEventListener("click", () => {
      state.resultView.dimensionIndex = dimensionIndex;
      state.resultView.questionIndex = 0;
      state.resultView.mode = "detail";
      renderResultDetail();
    });
    card.append(heading);

    const list = document.createElement("div");
    list.className = "result-subdimension-list";
    (dimension.questions || []).forEach((question, questionIndex) => {
      const status = resultAnswerCompletionStatus(result, flow, video, dimension, question);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `result-subdimension-button status-${status}`;
      button.classList.toggle(
        "is-selected",
        dimensionIndex === state.resultView.dimensionIndex && questionIndex === state.resultView.questionIndex,
      );
      button.setAttribute(
        "aria-label",
        `${question.prompt}, ${resultAnswerStatusText(status)}`,
      );
      const label = document.createElement("span");
      label.textContent = `${questionIndex + 1}. ${question.prompt}`;
      const statusLabel = document.createElement("strong");
      statusLabel.textContent = resultAnswerStatusText(status);
      button.append(label, statusLabel);
      button.addEventListener("click", () => {
        state.resultView.dimensionIndex = dimensionIndex;
        state.resultView.questionIndex = questionIndex;
        state.resultView.mode = "detail";
        renderResultDetail();
      });
      list.append(button);
    });
    card.append(list);
    subdimensionProgress.append(card);
  });
}

function resultScopeProgress(result, flow, video = null, dimension = null) {
  const videos = video ? [video] : resultVideos(flow, result);
  const dimensions = dimension ? [dimension] : flow?.dimensions || [];
  const progress = { total: 0, complete: 0, partial: 0, missing: 0, needsRevision: 0, started: 0, cancelled: 0 };
  for (const currentVideo of videos) {
    if (isCancelledVideo(currentVideo)) {
      progress.cancelled += 1;
      continue;
    }
    for (const currentDimension of dimensions) {
      for (const question of currentDimension.questions || []) {
        const status = resultAnswerCompletionStatus(result, flow, currentVideo, currentDimension, question);
        progress.total += 1;
        if (status === "complete") progress.complete += 1;
        if (status === "partial") progress.partial += 1;
        if (status === "missing") progress.missing += 1;
        if (status === "needs-revision") progress.needsRevision += 1;
      }
    }
  }
  progress.started = progress.total - progress.missing;
  return progress;
}

function resultAnswerCompletionStatus(result, flow, video, dimension, question) {
  if (isCancelledVideo(video)) return "cancelled";
  if (!video || !dimension || !question) return "missing";
  const key = answerKeyFor(video.id, dimension.id, question.id);
  const answer = result?.answers?.[key];
  const review = result?.answer_reviews?.[key];
  if (review?.status === "needs_revision") return "needs-revision";

  const explanationRequired = question.explanationRequired ?? flow?.responseConfig?.explanationRequired;
  const hasScore = answer?.score !== "" && answer?.score != null;
  const hasConfidence = answer?.confidence !== "" && answer?.confidence != null;
  const hasExplanation = Boolean(String(answer?.explanation || "").trim());
  if (hasScore && hasConfidence && (!explanationRequired || hasExplanation)) return "complete";
  if (hasScore || hasConfidence || hasExplanation) return "partial";
  return "missing";
}

function resultProgressStatus(progress) {
  if (!progress.total && progress.cancelled) return "cancelled";
  if (progress.total > 0 && progress.complete === progress.total) return "complete";
  if (progress.needsRevision > 0) return "needs-revision";
  if (progress.started > 0) return "partial";
  return "missing";
}

function resultProgressText(progress) {
  if (!progress.total && progress.cancelled) return "Cancelled";
  if (!progress.total) return "Not configured";
  if (progress.complete === progress.total) return `${progress.complete}/${progress.total} complete`;
  if (progress.needsRevision) return `${progress.complete}/${progress.total} | ${progress.needsRevision} revisions`;
  if (progress.started) return `${progress.complete}/${progress.total} | draft available`;
  return `0/${progress.total} | unanswered`;
}

function resultAnswerStatusText(status) {
  if (status === "cancelled") return "Cancelled";
  if (status === "complete") return "Complete";
  if (status === "partial") return "Draft";
  if (status === "needs-revision") return "Needs revision";
  return "Unanswered";
}

function renderNativeSelect(select, labels, selectedIndex) {
  select.innerHTML = "";
  select.disabled = labels.length === 0;
  if (!labels.length) {
    const option = document.createElement("option");
    option.value = "0";
    option.textContent = "Not configured";
    select.append(option);
    return;
  }
  labels.forEach((label, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = label;
    option.selected = index === selectedIndex;
    select.append(option);
  });
}

function renderResultVideo(video, flow) {
  const box = document.getElementById("resultVideoBox");
  const description = document.getElementById("resultVideoDescription");
  description.textContent = video?.description || "";
  if (video?.fileName) {
    box.innerHTML = `<video controls src="${escapeAttribute(videoSource(video.fileName, flow))}"></video>`;
    attachVideoPreview(box.querySelector("video"), video.fileName, flow);
  } else {
    removeVideoPreviewTimeline(box);
    box.textContent = "No filename is configured for this video.";
  }
}

function renderResultAnswer(result, video, dimension, question, flow) {
  const prompt = document.getElementById("resultQuestionPrompt");
  if (question) {
    renderQuestionPromptInto(prompt, question, flow);
  } else {
    prompt.innerHTML = "";
  }

  const key = video && dimension && question ? answerKeyFor(video.id, dimension.id, question.id) : "";
  if (isCancelledVideo(video)) {
    document.getElementById("resultScore").textContent = "Cancelled";
    document.getElementById("resultConfidence").textContent = "Excluded";
    document.getElementById("resultExplanation").textContent = cancelledVideoMessage(video);
    renderResultAnswerReview(result, "", {});
    return;
  }
  const answer = key ? result.answers?.[key] || {} : {};
  document.getElementById("resultScore").textContent = formatScore(answer.score, "Not rated");
  document.getElementById("resultConfidence").textContent = String(answer.confidence ?? "").trim() || "Not selected";
  renderResultExplanation(document.getElementById("resultExplanation"), answer);
  renderResultAnswerReview(result, key, answer);
}

function renderResultAnswerReview(result, key, answer) {
  const review = key ? result.answer_reviews?.[key] : null;
  const reviewBox = document.getElementById("resultAnswerReview");
  const button = document.getElementById("markAnswerForRevision");
  button.disabled = !key || !answer || typeof answer !== "object" || !Object.keys(answer).length;
  button.textContent = review?.status === "needs_revision" ? "Update Revision Note" : "Request Revision";
  if (!review) {
    reviewBox.hidden = true;
    reviewBox.textContent = "";
    return;
  }
  reviewBox.hidden = false;
  reviewBox.classList.toggle("is-resolved", review.status === "resolved");
  const title = review.status === "needs_revision" ? "Waiting for Revision" : "Revision Completed";
  reviewBox.innerHTML = `
    <strong>${escapeHtml(title)}</strong>
    <p>${escapeHtml(review.comment || "")}</p>
    <small>Requested: ${escapeHtml(formatDateTime(review.marked_at))}${review.resolved_at ? ` | Resolved: ${escapeHtml(formatDateTime(review.resolved_at))}` : ""}</small>
  `;
}

async function markCurrentResultAnswerForRevision() {
  const result = currentResult();
  if (!result) return;
  const flow = state.flow || {};
  const videos = resultVideos(flow, result);
  const video = videos[state.resultView.videoIndex];
  const dimension = (flow.dimensions || [])[state.resultView.dimensionIndex];
  const question = (dimension?.questions || [])[state.resultView.questionIndex];
  if (!video || !dimension || !question) return;
  if (isCancelledVideo(video)) {
    showAlert("This video is cancelled and cannot be marked for revision.");
    return;
  }
  const key = answerKeyFor(video.id, dimension.id, question.id);
  const answer = result.answers?.[key];
  if (!answer || typeof answer !== "object") {
    showAlert("This criterion has no answer and cannot be marked for revision.");
    return;
  }
  const currentComment = result.answer_reviews?.[key]?.comment || "";
  const comment = window.prompt("Enter a revision note for the participant:", currentComment);
  if (comment == null) return;
  if (!comment.trim()) {
    showAlert("The revision note cannot be empty.");
    return;
  }
  const response = await apiPost(
    `/api/submissions/${encodeURIComponent(result.id)}/answer-review`,
    { answer_key: key, comment: comment.trim() },
    { admin: true },
  );
  state.results[state.selectedResultIndex] = response.submission;
  renderResultDetail();
  showAlert("Revision requested. The original answer remains stored and the participant will see the note.", false);
}

function renderResultStats(result, flow) {
  const stats = calculateSubmissionStats(result, flow);
  const table = document.getElementById("resultStatsTable");
  table.innerHTML = "";
  if (!stats.dimensions.length) {
    table.innerHTML = '<tr><td colspan="5" class="empty-cell">This workflow has no dimensions.</td></tr>';
    return;
  }

  for (const dimension of stats.dimensions) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(dimension.title)}</td>
      <td>${escapeHtml(formatScore(dimension.averageScore))}</td>
      <td>${escapeHtml(formatScore(dimension.totalScore))}</td>
      <td>${dimension.scoredCount}/${dimension.expectedCount}</td>
      <td>${escapeHtml(scoreBreakdownText(dimension.scores))}</td>
    `;
    table.append(row);
  }

  const totalRow = document.createElement("tr");
  totalRow.className = "result-total-row";
  totalRow.innerHTML = `
    <td>Total</td>
    <td>${escapeHtml(formatScore(stats.totalAverage))}</td>
    <td>${escapeHtml(formatScore(stats.totalScore))}</td>
    <td>${stats.scoredCount}/${stats.expectedCount}</td>
    <td>Scores only; confidence is excluded</td>
  `;
  table.append(totalRow);
}

function renderResultMode() {
  const isStats = state.resultView.mode === "stats";
  document.getElementById("resultDetailViewer").hidden = isStats;
  document.getElementById("resultStatsPanel").hidden = !isStats;
  document.getElementById("resultDetailViewTab").classList.toggle("is-active", !isStats);
  document.getElementById("resultStatsTab").classList.toggle("is-active", isStats);
}

function setResultDetailMode(mode) {
  state.resultView.mode = mode === "stats" ? "stats" : "detail";
  renderResultDetail();
}

function selectResultVideo(index) {
  const videos = resultVideos(state.flow, currentResult());
  if (index < 0 || index >= videos.length) return;
  state.resultView.videoIndex = index;
  renderResultDetail();
}

function selectResultDimension(index) {
  const dimensions = state.flow?.dimensions || [];
  if (index < 0 || index >= dimensions.length) return;
  state.resultView.dimensionIndex = index;
  state.resultView.questionIndex = 0;
  renderResultDetail();
}

function selectResultQuestion(index) {
  const questions = (state.flow?.dimensions || [])[state.resultView.dimensionIndex]?.questions || [];
  if (index < 0 || index >= questions.length) return;
  state.resultView.questionIndex = index;
  renderResultDetail();
}

function moveResultVideo(delta) {
  selectResultVideo(state.resultView.videoIndex + delta);
}

function moveResultDimension(delta) {
  selectResultDimension(state.resultView.dimensionIndex + delta);
}

function moveResultQuestion(delta) {
  selectResultQuestion(state.resultView.questionIndex + delta);
}

function clampResultView() {
  const videos = resultVideos(state.flow, currentResult());
  const dimensions = state.flow?.dimensions || [];
  state.resultView.videoIndex = clampIndex(state.resultView.videoIndex, videos.length);
  state.resultView.dimensionIndex = clampIndex(state.resultView.dimensionIndex, dimensions.length);
  const questions = dimensions[state.resultView.dimensionIndex]?.questions || [];
  state.resultView.questionIndex = clampIndex(state.resultView.questionIndex, questions.length);
}

function clampIndex(value, length) {
  if (!length) return 0;
  const index = Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : 0;
  return Math.min(Math.max(index, 0), length - 1);
}

function updateResultNavigationButtons(video, dimension, question) {
  const videos = resultVideos(state.flow, currentResult());
  const dimensions = state.flow?.dimensions || [];
  const questions = dimension?.questions || [];
  document.getElementById("prevResultVideo").disabled = !video || state.resultView.videoIndex <= 0;
  document.getElementById("nextResultVideo").disabled = !video || state.resultView.videoIndex >= videos.length - 1;
  document.getElementById("prevResultDimension").disabled = !dimension || state.resultView.dimensionIndex <= 0;
  document.getElementById("nextResultDimension").disabled =
    !dimension || state.resultView.dimensionIndex >= dimensions.length - 1;
  document.getElementById("prevResultQuestion").disabled = !question || state.resultView.questionIndex <= 0;
  document.getElementById("nextResultQuestion").disabled =
    !question || state.resultView.questionIndex >= questions.length - 1;
}

function resultVideos(flow, submission = null) {
  return videosForFlow(flow, submission?.video_order || []);
}

function pendingReviewCount(submission) {
  return Object.values(submission?.answer_reviews || {}).filter(
    (review) => review && review.status === "needs_revision",
  ).length;
}

function calculateSubmissionStats(submission, flow = state.flow) {
  const answers = submission?.answers || {};
  const videos = resultVideos(flow, submission).filter((video) => !isCancelledVideo(video));
  const dimensions = (flow?.dimensions || []).map((dimension) => ({
    id: dimension.id,
    title: dimension.title,
    totalScore: 0,
    averageScore: null,
    scoredCount: 0,
    expectedCount: videos.length * (dimension.questions || []).length,
    scores: [],
  }));

  const dimensionStatsById = new Map(dimensions.map((dimension) => [dimension.id, dimension]));
  for (const video of videos) {
    for (const dimension of flow?.dimensions || []) {
      const dimensionStats = dimensionStatsById.get(dimension.id);
      if (!dimensionStats) continue;
      for (const question of dimension.questions || []) {
        const key = answerKeyFor(video.id, dimension.id, question.id);
        const score = numericAnswerScore(answers[key]);
        if (score == null) continue;
        dimensionStats.totalScore += score;
        dimensionStats.scoredCount += 1;
        dimensionStats.scores.push({
          videoTitle: video.title,
          questionPrompt: question.prompt,
          score,
        });
      }
    }
  }

  let totalScore = 0;
  let scoredCount = 0;
  let expectedCount = 0;
  for (const dimension of dimensions) {
    totalScore += dimension.totalScore;
    scoredCount += dimension.scoredCount;
    expectedCount += dimension.expectedCount;
    dimension.averageScore = dimension.scoredCount ? dimension.totalScore / dimension.scoredCount : null;
  }

  return {
    dimensions,
    totalScore,
    totalAverage: scoredCount ? totalScore / scoredCount : null,
    scoredCount,
    expectedCount,
  };
}

function numericAnswerScore(answer) {
  if (!answer || answer.score === "" || answer.score == null) return null;
  const score = Number(answer.score);
  return Number.isFinite(score) ? score : null;
}

function scoreBreakdownText(scores) {
  if (!scores.length) return "None";
  return scores
    .map((item) => `${item.videoTitle} / ${item.questionPrompt}: ${formatScore(item.score)}`)
    .join("; ");
}

function isCancelledVideo(video) {
  return Boolean(
    video?.cancelled ||
      video?.isCancelled ||
      String(video?.status || "").trim().toLowerCase() === "cancelled",
  );
}

function isCancelledItem(item) {
  return isCancelledVideo(item?.video);
}

function cancelledVideoMessage(video) {
  return String(video?.cancelMessage || video?.cancelReason || "This video has been cancelled");
}

function isCancelledAnswerKey(key, flow = state.flow) {
  const videoId = String(key || "").split(":", 1)[0];
  return (flow?.videos || []).some((video) => video.id === videoId && isCancelledVideo(video));
}

function sanitizeCancelledAnswers() {
  const cancelledKeys = Object.keys(state.answers || {}).filter((key) => isCancelledAnswerKey(key));
  for (const key of cancelledKeys) {
    delete state.answers[key];
    state.dirtyAnswerKeys.delete(key);
  }
}

function answerKeyFor(videoId, dimensionId, questionId) {
  return `${videoId}:${dimensionId}:${questionId}`;
}

function statusLabel(status) {
  if (status === "draft") return "Draft";
  if (status === "submitted") return "Submitted";
  return status || "Submitted";
}

function flowStatusLabel(status) {
  return status === "published" ? "Published" : status === "draft" ? "Draft" : String(status || "Unknown status");
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("en-US", { hour12: false });
}

function formatScore(value, emptyText = "None") {
  if (value === "" || value == null) return emptyText;
  const number = Number(value);
  if (!Number.isFinite(number)) return emptyText;
  return number.toFixed(2).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

async function downloadCsv() {
  if (!(await ensureAdminAccess())) return;
  const query = state.flow ? `?flow_id=${encodeURIComponent(state.flow.id)}` : "";
  const response = await fetch(`/api/submissions/export.csv${query}`, {
    headers: adminHeaders(),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "human-eval-platform-results.csv";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function flattenQuestions(flow, videoOrder = []) {
  const items = [];
  videosForFlow(flow, videoOrder).forEach((video, videoIndex) => {
    (flow.dimensions || []).forEach((dimension, dimensionIndex) => {
      (dimension.questions || []).forEach((question, questionIndex) => {
        items.push({
          video,
          videoIndex,
          dimension,
          dimensionIndex,
          question,
          questionIndex,
          answerKey: answerKeyFor(video.id, dimension.id, question.id),
        });
      });
    });
  });
  return items;
}

function videosForFlow(flow, videoOrder = []) {
  const videos = flow?.videos || [];
  if (!Array.isArray(videoOrder) || !videoOrder.length) return videos;
  const byId = new Map(videos.map((video) => [video.id, video]));
  const ordered = videoOrder.map((videoId) => byId.get(videoId)).filter(Boolean);
  const used = new Set(ordered.map((video) => video.id));
  return [...ordered, ...videos.filter((video) => !used.has(video.id))];
}

function participantVideos() {
  return videosForFlow(state.flow, state.videoOrder);
}

function videoSource(path, flow = state.flow) {
  const rawPath = cleanMediaPath(path);
  if (!rawPath) return "";
  if (/^https?:\/\//.test(rawPath)) return rawPath;
  return `/videos/${mediaRelativePath(rawPath, flow)}`;
}

function mediaRelativePath(path, flow = state.flow) {
  const rawPath = cleanMediaPath(path);
  if (!rawPath || /^https?:\/\//.test(rawPath)) return rawPath;
  if (rawPath.includes("/") || !flow?.videoFolder) return rawPath;
  return `${flow.videoFolder}/${rawPath}`;
}

function cleanMediaPath(path) {
  return String(path || "")
    .trim()
    .replace(/^\/videos\//, "")
    .replace(/^\/+/, "");
}

function removeVideoPreviewTimeline(videoBox) {
  const timeline = videoBox?.nextElementSibling;
  if (timeline?.classList.contains("video-preview-timeline")) timeline.remove();
}

function attachVideoPreview(video, path, flow = state.flow, options = {}) {
  if (!video) return;
  const videoBox = video.closest(".video-box");
  if (!videoBox) return;
  removeVideoPreviewTimeline(videoBox);
  const videoPath = mediaRelativePath(path, flow);
  if (!videoPath || /^https?:\/\//.test(videoPath)) return;

  const timeline = document.createElement("div");
  timeline.className = "video-preview-timeline";
  timeline.hidden = true;
  timeline.dataset.videoPath = videoPath;
  timeline.setAttribute("aria-label", "Video frame preview timeline");
  timeline.title = "Move the pointer to preview a frame; click the timeline to seek";
  timeline.innerHTML = `
    <div class="video-preview-rail">
      <div class="video-preview-played"></div>
      <div class="video-preview-cursor"></div>
    </div>
    <div class="video-preview-popover" hidden>
      <div class="video-preview-frame"></div>
      <span class="video-preview-time"></span>
    </div>
    ${
      options.allowVideoReference && options.videoId
        ? `<div class="video-preview-actions">
            <button class="secondary video-time-reference-button" type="button">
              Quote Current Time <span class="video-time-reference-value">0:00</span>
            </button>
          </div>`
        : ""
    }
  `;
  if (options.videoId) timeline.dataset.videoId = String(options.videoId);
  videoBox.insertAdjacentElement("afterend", timeline);

  loadVideoPreviewManifest(videoPath)
    .then((manifest) => {
      if (!timeline.isConnected || timeline.dataset.videoPath !== videoPath) return;
      configureVideoPreviewTimeline(video, timeline, manifest);
    })
    .catch(() => {
      if (timeline.isConnected && timeline.dataset.videoPath === videoPath) timeline.remove();
    });
}

function loadVideoPreviewManifest(videoPath) {
  if (!state.videoPreviewManifests.has(videoPath)) {
    const params = new URLSearchParams({ video_path: videoPath });
    const request = fetch(`/api/video-preview?${params.toString()}`).then(async (response) => {
      if (!response.ok) throw new Error(`Video preview request failed: HTTP ${response.status}`);
      return response.json();
    });
    state.videoPreviewManifests.set(videoPath, request);
    request.catch(() => state.videoPreviewManifests.delete(videoPath));
  }
  return state.videoPreviewManifests.get(videoPath);
}

function configureVideoPreviewTimeline(video, timeline, manifest) {
  const interval = Number(manifest.intervalSeconds);
  const frameCount = Number(manifest.frameCount);
  const width = Number(manifest.thumbWidth);
  const height = Number(manifest.thumbHeight);
  const displayWidth = 240;
  const displayHeight = 136;
  const columns = Number(manifest.columns);
  const rows = Number(manifest.rows);
  const framesPerSheet = Number(manifest.framesPerSheet);
  const sheets = Array.isArray(manifest.sheets) ? manifest.sheets : [];
  if (
    !interval ||
    !frameCount ||
    !width ||
    !height ||
    !columns ||
    !rows ||
    !framesPerSheet ||
    !sheets.length ||
    !manifest.assetsBasePath
  ) {
    timeline.remove();
    return;
  }

  const rail = timeline.querySelector(".video-preview-rail");
  const played = timeline.querySelector(".video-preview-played");
  const cursor = timeline.querySelector(".video-preview-cursor");
  const popover = timeline.querySelector(".video-preview-popover");
  const frame = timeline.querySelector(".video-preview-frame");
  const timeLabel = timeline.querySelector(".video-preview-time");
  const referenceButton = timeline.querySelector(".video-time-reference-button");
  const referenceTime = timeline.querySelector(".video-time-reference-value");
  const manifestDuration = Number(manifest.duration);
  frame.style.width = `${displayWidth}px`;
  frame.style.height = `${displayHeight}px`;

  const videoDuration = () =>
    Number.isFinite(video.duration) && video.duration > 0 ? video.duration : manifestDuration;
  const updatePlayed = () => {
    const duration = videoDuration();
    const ratio = duration > 0 ? Math.min(Math.max(video.currentTime / duration, 0), 1) : 0;
    played.style.width = `${ratio * 100}%`;
    if (referenceTime) referenceTime.textContent = formatVideoPreviewTime(video.currentTime);
  };
  const previewPosition = (event) => {
    const rect = rail.getBoundingClientRect();
    const duration = videoDuration();
    if (!rect.width || !duration) return null;
    const offset = Math.min(Math.max(event.clientX - rect.left, 0), rect.width);
    const ratio = offset / rect.width;
    const pointerTime = Math.min(duration, ratio * duration);
    const frameIndex = Math.min(frameCount - 1, Math.floor(pointerTime / interval));
    const time = Math.min(duration, frameIndex * interval);
    return { duration, frameIndex, offset, ratio, rect, time };
  };
  const updatePreview = (event) => {
    const position = previewPosition(event);
    if (!position) return;
    const {
      frameIndex,
      offset,
      ratio,
      rect,
      time,
    } = position;
    const sheetIndex = Math.floor(frameIndex / framesPerSheet);
    const cellIndex = frameIndex % framesPerSheet;
    const sheet = sheets[sheetIndex];
    if (!sheet) return;
    const column = cellIndex % columns;
    const row = Math.floor(cellIndex / columns);
    const assetVersion = encodeURIComponent(
      `${manifest.sourceSize || ""}-${manifest.sourceMtimeNs || ""}-${interval}-${width}x${height}`,
    );
    const assetUrl = `${manifest.assetsBasePath}${encodeURIComponent(sheet)}?v=${assetVersion}`;
    frame.style.backgroundImage = `url("${assetUrl}")`;
    frame.style.backgroundSize = `${displayWidth * columns}px ${displayHeight * rows}px`;
    frame.style.backgroundPosition = `${-column * displayWidth}px ${-row * displayHeight}px`;
    timeLabel.textContent = formatVideoPreviewTime(time);
    cursor.style.left = `${ratio * 100}%`;
    const halfPopover = Math.min(displayWidth / 2 + 6, rect.width / 2);
    const left = Math.min(Math.max(offset, halfPopover), rect.width - halfPopover);
    popover.style.left = `${left}px`;
    popover.hidden = false;
    cursor.hidden = false;
  };

  rail.addEventListener("pointerenter", updatePreview);
  rail.addEventListener("pointermove", updatePreview);
  rail.addEventListener("click", (event) => {
    const position = previewPosition(event);
    if (!position) return;
    seekVideoToTime(video, position.time);
    updatePlayed();
  });
  referenceButton?.addEventListener("click", () => {
    addVideoTimeReference(timeline.dataset.videoId, video.currentTime);
  });
  timeline.addEventListener("pointerleave", () => {
    popover.hidden = true;
    cursor.hidden = true;
  });
  video.addEventListener("timeupdate", updatePlayed);
  video.addEventListener("durationchange", updatePlayed);
  video.addEventListener("loadedmetadata", updatePlayed);
  cursor.hidden = true;
  updatePlayed();
  timeline.hidden = false;
}

function seekVideoToTime(video, value) {
  if (!video) return false;
  let target = Math.max(0, Number(value) || 0);
  if (Number.isFinite(video.duration) && video.duration > 0) {
    target = Math.min(target, video.duration);
  }
  try {
    video.currentTime = target;
    return true;
  } catch {
    return false;
  }
}

function formatVideoPreviewTime(value) {
  const totalSeconds = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const minuteText = hours ? String(minutes).padStart(2, "0") : String(minutes);
  return `${hours ? `${hours}:` : ""}${minuteText}:${String(seconds).padStart(2, "0")}`;
}

function showView(name) {
  document.body.classList.toggle("evaluation-active", name === "evaluation");
  for (const view of views) {
    document.getElementById(`${view}View`).hidden = view !== name;
  }
  document.querySelectorAll(".tab[data-view]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === name);
  });
  if (name === "evaluation") {
    renderEvaluation();
    renderUsagePolicyBanner();
    startUsagePolicyPolling();
  } else {
    stopUsagePolicyPolling();
  }
}

function showAlert(message, isError = true) {
  const alert = document.getElementById("alert");
  alert.hidden = false;
  alert.textContent = message;
  alert.style.borderColor = isError ? "#f0c2bd" : "#badbcc";
  alert.style.background = isError ? "#fff4f2" : "#f2fff6";
  alert.style.color = isError ? "#b42318" : "#176b5c";
}

function clearAlert() {
  document.getElementById("alert").hidden = true;
}

async function apiGet(path, options = {}) {
  const response = await fetch(path, {
    headers: options.admin ? adminHeaders() : {},
  });
  return readApiResponse(response);
}

async function apiPost(path, payload, options = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(options.admin ? adminHeaders() : {}),
    },
    body: JSON.stringify(payload),
  });
  return readApiResponse(response);
}

function adminHeaders() {
  return {
    "X-Admin-Password": sessionStorage.getItem(ADMIN_PASSWORD_STORAGE_KEY) || "",
  };
}

async function readApiResponse(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function draftKey() {
  if (!state.flow) return "human-eval-draft";
  return `human-eval-draft:${state.flow.id}:${state.participantKey || "anonymous"}`;
}

function saveDraft() {
  if (!state.flow) return;
  sanitizeCancelledAnswers();
  localStorage.setItem(
    draftKey(),
    JSON.stringify({
      participant: state.participant,
      answers: state.answers,
      currentIndex: state.currentIndex,
      updatedAt: Date.now(),
    }),
  );
}

function restoreDraft() {
  const raw = localStorage.getItem(draftKey());
  if (!raw) return null;
  try {
    const draft = JSON.parse(raw);
    state.participant = draft.participant || {};
    state.answers = draft.answers || {};
    state.currentIndex = draft.currentIndex || 0;
    return draft;
  } catch {
    localStorage.removeItem(draftKey());
    return null;
  }
}

function restoreDraftIfUseful(submission) {
  const draft = restoreDraft();
  if (!draft) return null;
  const serverAnswers = submission?.answers || {};
  const serverUpdatedAt = Date.parse(submission?.updated_at || submission?.created_at || "") || 0;
  const localUpdatedAt = Number(draft.updatedAt || 0);
  if (!Object.keys(serverAnswers).length || localUpdatedAt > serverUpdatedAt) {
    sanitizeCancelledAnswers();
    state.dirtyAnswerKeys = new Set(answerDifferenceKeys(state.answers, serverAnswers));
    state.currentIndex = firstIncompleteIndex();
    return draft;
  }
  state.participant = submission?.participant || state.participant;
  state.answers = serverAnswers;
  sanitizeCancelledAnswers();
  state.dirtyAnswerKeys = new Set();
  state.currentIndex = firstIncompleteIndex();
  return null;
}

function answerDifferenceKeys(left, right) {
  const keys = new Set([...Object.keys(left || {}), ...Object.keys(right || {})]);
  return [...keys].filter((key) => !answerValuesEqual(left?.[key], right?.[key]));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return encodeURIComponent(String(value)).replaceAll("%2F", "/");
}
