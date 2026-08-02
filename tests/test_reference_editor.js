const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { parseHTML } = require("linkedom");

const projectRoot = path.resolve(__dirname, "..");
const { document, window } = parseHTML(
  fs.readFileSync(path.join(projectRoot, "static/index.html"), "utf8"),
);

window.getSelection = () => null;

class TestRange {
  selectNodeContents(node) {
    this.startContainer = node;
    this.startOffset = 0;
    this.endContainer = node;
    this.endOffset = node.childNodes.length;
  }

  setStart(node, offset) {
    this.startContainer = node;
    this.startOffset = offset;
    this.endContainer = node;
    this.endOffset = offset;
  }

  setStartAfter(node) {
    const parent = node.parentNode;
    this.setStart(parent, Array.from(parent.childNodes).indexOf(node) + 1);
  }

  collapse() {}

  cloneRange() {
    const range = new TestRange();
    Object.assign(range, this);
    return range;
  }
}

document.createRange = () => new TestRange();

const sandbox = {
  window,
  document,
  Node: window.Node,
  Event: window.Event,
  console,
  setTimeout,
  clearTimeout,
  URL,
  localStorage: {
    setItem() {},
    getItem() {
      return null;
    },
    removeItem() {},
  },
  fetch: async () => {
    throw new Error("Network requests are not expected during this test");
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const appSource = fs.readFileSync(path.join(projectRoot, "static/app.js"), "utf8");
vm.runInContext(
  `${appSource}
globalThis.__referenceTest = {
  state,
  renderExplanationEditor,
  renderResultExplanation,
  syncCurrentAnswer,
  escapeHtml,
  referenceChipAdjacentToCaret,
  bindExplanationEditor,
  configureVideoPreviewTimeline,
  resultVideos,
  calculateSubmissionStats,
};`,
  sandbox,
  { filename: "static/app.js" },
);

const {
  state,
  renderExplanationEditor,
  renderResultExplanation,
  syncCurrentAnswer,
  escapeHtml,
  referenceChipAdjacentToCaret,
  bindExplanationEditor,
  configureVideoPreviewTimeline,
  resultVideos,
  calculateSubmissionStats,
} = sandbox.__referenceTest;

const attributeAnswerKey = "video:dimension:question\"&";
const attributeFixture = document.createElement("div");
attributeFixture.innerHTML = (
  `<div data-answer-key="${escapeHtml(attributeAnswerKey)}"></div>`
);
assert.equal(attributeFixture.firstElementChild.dataset.answerKey, attributeAnswerKey);
assert.match(
  appSource,
  /data-answer-key="\$\{escapeHtml\(current\.answerKey\)\}"/u,
);

const resultFlow = {
  videos: [
    { id: "video-a", title: "Video A" },
    { id: "video-b", title: "Video B" },
    { id: "video-c", title: "Video C" },
  ],
  dimensions: [
    {
      id: "quality",
      title: "Visual Quality",
      questions: [{ id: "clarity", prompt: "Is it clear?" }],
    },
  ],
};
const orderedResult = {
  video_order: ["video-c", "missing-video", "video-a"],
  answers: {
    "video-a:quality:clarity": { score: "1" },
    "video-b:quality:clarity": { score: "2" },
    "video-c:quality:clarity": { score: "3" },
  },
};
assert.deepEqual(
  JSON.parse(JSON.stringify(
    resultVideos(resultFlow, orderedResult).map((video) => video.id),
  )),
  ["video-c", "video-a", "video-b"],
);
assert.deepEqual(
  JSON.parse(JSON.stringify(
    calculateSubmissionStats(orderedResult, resultFlow)
      .dimensions[0].scores
      .map((score) => score.videoTitle),
  )),
  ["Video C", "Video A", "Video B"],
);

const answerKey = "video:dimension:question";
state.flatQuestions = [{ answerKey, video: { id: "video" } }];
state.currentIndex = 0;
state.flow = null;
state.answers[answerKey] = {
  score: "4",
  confidence: "4",
  explanation_body: "BeforeAfter",
  references: [
    {
      id: "ref-01",
      text: "Complete quoted source",
      source_key: "sample.txt",
      start: 30,
      end: 36,
      source_length: 100,
    },
  ],
  reference_placements: [{ reference_id: "ref-01", offset: 6 }],
  explanation: "Before'''[30%]: Complete quoted source'''After",
};

document.getElementById("videoSelectButton").remove();
const scoreInput = document.createElement("input");
scoreInput.id = "scoreInput";
scoreInput.value = "4";
const confidenceInput = document.createElement("input");
confidenceInput.id = "confidenceInput";
confidenceInput.value = "4";
const explanationInput = document.createElement("div");
explanationInput.id = "explanationInput";
explanationInput.dataset.answerKey = answerKey;
explanationInput.contentEditable = "true";
document.body.append(scoreInput, confidenceInput, explanationInput);

renderExplanationEditor(explanationInput, state.answers[answerKey]);
assert.equal(
  explanationInput.querySelectorAll("[data-answer-reference-id]").length,
  1,
);
const renderedChip = explanationInput.querySelector("[data-answer-reference-id]");
assert.equal(renderedChip.querySelectorAll("[data-remove-reference]").length, 1);

const beforeReference = explanationInput.firstChild;
const afterReference = explanationInput.lastChild;
window.getSelection = () => ({
  rangeCount: 1,
  getRangeAt: () => ({
    collapsed: true,
    startContainer: afterReference,
    endContainer: afterReference,
    startOffset: 0,
    endOffset: 0,
  }),
});
assert.equal(
  referenceChipAdjacentToCaret(explanationInput, "Backspace"),
  renderedChip,
);
window.getSelection = () => ({
  rangeCount: 1,
  getRangeAt: () => ({
    collapsed: true,
    startContainer: beforeReference,
    endContainer: beforeReference,
    startOffset: beforeReference.nodeValue.length,
    endOffset: beforeReference.nodeValue.length,
  }),
});
assert.equal(
  referenceChipAdjacentToCaret(explanationInput, "Delete"),
  renderedChip,
);
window.getSelection = () => null;

// Simulate a contenteditable mutation that removes the non-editable reference chip.
explanationInput.textContent = "BeforeAfter";
syncCurrentAnswer();

assert.equal(state.answers[answerKey].references.length, 1);
assert.deepEqual(
  JSON.parse(JSON.stringify(state.answers[answerKey].reference_placements)),
  [{ reference_id: "ref-01", offset: 6 }],
);
assert.equal(
  explanationInput.querySelectorAll("[data-answer-reference-id]").length,
  1,
);
assert.equal(
  state.answers[answerKey].explanation,
  "Before'''[30%]: Complete quoted source'''After",
);

const resultExplanation = document.createElement("div");
renderResultExplanation(resultExplanation, state.answers[answerKey]);
assert.equal(
  resultExplanation.textContent,
  "Before'''[30%]: Complete quoted source'''After",
);

state.flow = {
  id: "test-flow",
  videos: [],
  responseConfig: {},
};
state.flatQuestions[0].question = {};
bindExplanationEditor(state.flatQuestions[0], explanationInput);
explanationInput.querySelector("[data-remove-reference]").click();
assert.equal(state.answers[answerKey].references.length, 0);
assert.deepEqual(
  JSON.parse(JSON.stringify(state.answers[answerKey].reference_placements)),
  [],
);
assert.equal(state.answers[answerKey].explanation, "BeforeAfter");
assert.equal(state.dirtyAnswerKeys.has(answerKey), true);

const videoReference = {
  id: "ref-video-01",
  type: "video_time",
  video_id: "video",
  time_seconds: 80,
};
state.answers[answerKey] = {
  score: "4",
  confidence: "4",
  explanation_body: "BeforeAfter",
  references: [videoReference],
  reference_placements: [{ reference_id: "ref-video-01", offset: 6 }],
  explanation: "Before'''Video[1:20]'''After",
};
renderExplanationEditor(explanationInput, state.answers[answerKey]);
const videoReferenceChip = explanationInput.querySelector("[data-answer-reference-id]");
assert.equal(videoReferenceChip.classList.contains("is-video-reference"), true);
assert.equal(videoReferenceChip.textContent.includes("Reference 01: Video 1:20"), true);
assert.equal(videoReferenceChip.querySelectorAll("[data-remove-reference]").length, 1);
renderResultExplanation(resultExplanation, state.answers[answerKey]);
assert.equal(resultExplanation.textContent, "Before'''Video[1:20]'''After");

const participantVideoContainer = document.getElementById("videoContainer");
participantVideoContainer.innerHTML = "<video></video>";
const participantVideo = participantVideoContainer.querySelector("video");
Object.defineProperty(participantVideo, "duration", {
  configurable: true,
  value: 120,
});
Object.defineProperty(participantVideo, "currentTime", {
  configurable: true,
  writable: true,
  value: 0,
});
videoReferenceChip.querySelector("[data-highlight-reference]").click();
assert.equal(participantVideo.currentTime, 80);

videoReferenceChip.querySelector("[data-remove-reference]").click();
assert.equal(state.answers[answerKey].references.length, 0);
assert.deepEqual(
  JSON.parse(JSON.stringify(state.answers[answerKey].reference_placements)),
  [],
);
assert.equal(state.answers[answerKey].explanation, "BeforeAfter");

const previewTimeline = document.createElement("div");
previewTimeline.innerHTML = `
  <div class="video-preview-rail">
    <div class="video-preview-played"></div>
    <div class="video-preview-cursor"></div>
  </div>
  <div class="video-preview-popover" hidden>
    <div class="video-preview-frame"></div>
    <span class="video-preview-time"></span>
  </div>
`;
document.body.append(previewTimeline);
const previewRail = previewTimeline.querySelector(".video-preview-rail");
previewRail.getBoundingClientRect = () => ({
  left: 0,
  width: 100,
});
participantVideo.currentTime = 0;
configureVideoPreviewTimeline(participantVideo, previewTimeline, {
  intervalSeconds: 1,
  frameCount: 100,
  thumbWidth: 320,
  thumbHeight: 180,
  columns: 10,
  rows: 10,
  framesPerSheet: 100,
  sheets: ["sheet-0000.jpg"],
  assetsBasePath: "/video-previews/example/",
  duration: 100,
});
const pointerMove = new window.Event("pointermove", { bubbles: true });
Object.defineProperty(pointerMove, "clientX", { value: 50 });
previewRail.dispatchEvent(pointerMove);
assert.equal(participantVideo.currentTime, 0);
const timelineClick = new window.Event("click", { bubbles: true });
Object.defineProperty(timelineClick, "clientX", { value: 50 });
previewRail.dispatchEvent(timelineClick);
assert.equal(participantVideo.currentTime, 60);

console.log("Reference editor regression test passed");
