/* IECMS Companion — chat front end.
 *
 * Plain vanilla JavaScript, no dependencies and no third-party calls. The
 * script manages the interface states (empty, loading, error, answer) and
 * sends the question to the backend, which is the only place that talks to any
 * external service.
 *
 * Answers are inserted as text (never as HTML) so that content returned by the
 * backend cannot inject markup into the page.
 *
 * The conversation is held in memory only. Nothing is written to
 * sessionStorage, localStorage or a cookie. See docs/PRIVACY.md.
 */
(function () {
  "use strict";

  var form = document.getElementById("chat-form");
  if (!form) {
    return;
  }

  var log = document.getElementById("chat-log");
  var empty = document.getElementById("chat-empty");
  var statusEl = document.getElementById("chat-status");
  var errorEl = document.getElementById("chat-error");
  var input = document.getElementById("question");
  var sendButton = document.getElementById("send");
  var counter = document.getElementById("composer-counter");

  var endpoint = form.getAttribute("data-endpoint");
  var summariseEndpoint = form.getAttribute("data-summarise-endpoint");
  var maxLength = parseInt(form.getAttribute("data-max-length"), 10) || 500;
  var maxUploadBytes =
    parseInt(form.getAttribute("data-max-upload-bytes"), 10) || 0;
  var csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
  var uploadInput = document.getElementById("document-upload");
  var uploadButton = document.getElementById("upload-button");
  var uploadName = document.getElementById("upload-name");

  /* --- Conversation state ------------------------------------------------ */

  // Older exchanges are dropped so a long session stays bounded. This is
  // memory only and is discarded when the page is reloaded or closed.
  var MAX_EXCHANGES = 20;

  var exchanges = [];

  function recordExchange(question, payload, article) {
    exchanges.push({ question: question, payload: payload, article: article });
    if (exchanges.length > MAX_EXCHANGES) {
      exchanges = exchanges.slice(-MAX_EXCHANGES);
    }
  }

  /* --- Theme toggle ------------------------------------------------------ */

  var themeToggle = document.getElementById("theme-toggle");
  if (themeToggle) {
    function applyTheme(theme) {
      document.documentElement.setAttribute("data-theme", theme);
      try { localStorage.setItem("theme", theme); } catch (e) { /* ignore */ }
      themeToggle.textContent =
        theme === "dark" ? "\u2600\uFE0F Light" : "\uD83C\uDF19 Dark";
    }

    var currentTheme =
      document.documentElement.getAttribute("data-theme") === "dark"
        ? "dark"
        : "light";
    applyTheme(currentTheme);

    themeToggle.addEventListener("click", function () {
      var next =
        document.documentElement.getAttribute("data-theme") === "dark"
          ? "light"
          : "dark";
      applyTheme(next);
    });
  }

  var STRINGS = {
    you: "You",
    assistant: "IECMS Companion",
    sources: "Sources",
    listen: "\uD83D\uDD0A Listen",
    unsupported: "\uD83D\uDD07 Listen (unsupported)",
    thinking: "Looking up the answer\u2026",
    networkError:
      "I could not reach the service. Check your connection and try again.",
    genericError: "Something went wrong. Please try again."
  };

  /* --- Small DOM helpers ------------------------------------------------- */

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (typeof text === "string") {
      node.textContent = text;
    }
    return node;
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  function clearError() {
    errorEl.textContent = "";
    errorEl.hidden = true;
  }

  function setStatus(message) {
    if (message) {
      statusEl.textContent = message;
      statusEl.hidden = false;
    } else {
      statusEl.textContent = "";
      statusEl.hidden = true;
    }
  }

  function hideEmptyState() {
    if (empty && !empty.hidden) {
      empty.hidden = true;
    }
  }

  function setBusy(busy) {
    sendButton.disabled = busy || !input.value.trim();
    input.setAttribute("aria-busy", busy ? "true" : "false");
  }

  /* --- Speech synthesis -------------------------------------------------- */

  function speechSupported() {
    return (
      typeof window.speechSynthesis !== "undefined" &&
      typeof window.SpeechSynthesisUtterance !== "undefined"
    );
  }

  function preferredVoice() {
    var voices = window.speechSynthesis.getVoices() || [];
    for (var i = 0; i < voices.length; i += 1) {
      if (voices[i].lang && voices[i].lang.toLowerCase().indexOf("en") === 0) {
        return voices[i];
      }
    }
    return null;
  }

  function buildSpeakerButton(text) {
    var button = make("button", "speak", STRINGS.listen);
    button.type = "button";

    if (!speechSupported()) {
      button.disabled = true;
      button.textContent = STRINGS.unsupported;
      button.title = "Speech is not supported in this browser.";
      return button;
    }

    button.addEventListener("click", function () {
      window.speechSynthesis.cancel();
      var utterance = new window.SpeechSynthesisUtterance(text);
      var voice = preferredVoice();
      if (voice) {
        utterance.voice = voice;
        utterance.lang = voice.lang;
      } else {
        // No English voice on this device: fall back and tell the user.
        button.title =
          "No English voice is installed on this device; using the default " +
          "voice.";
      }
      window.speechSynthesis.speak(utterance);
    });

    return button;
  }

  /* --- Rendering --------------------------------------------------------- */

  function buildSources(sources) {
    var section = make("section", "sources");
    section.appendChild(make("h3", "sources__title", STRINGS.sources));

    var list = make("ul", "sources__list");
    sources.forEach(function (source) {
      var item = make("li");

      // The stored local document is preferred because it can be opened at
      // the cited page. A remote URL is used only when no local copy exists.
      var href = source.local_url || source.source_url || "";
      if (source.local_url && source.page_number) {
        href += "#page=" + encodeURIComponent(source.page_number);
      }
      var titleText = source.document_title || "Untitled source";
      if (href) {
        var titleLink = make("a", "source__title source__title--link", titleText);
        titleLink.href = href;
        titleLink.rel = "noopener noreferrer";
        titleLink.target = "_blank";
        item.appendChild(titleLink);
      } else {
        item.appendChild(make("span", "source__title", titleText));
      }

      var meta = [];
      if (source.issuing_authority) {
        meta.push(source.issuing_authority);
      }
      if (source.page_number) {
        meta.push("p. " + source.page_number);
      }
      if (source.section_heading) {
        meta.push(source.section_heading);
      }
      if (source.verification_status) {
        meta.push("(" + source.verification_status + ")");
      }
      if (meta.length) {
        item.appendChild(make("div", "source__meta", meta.join(" \u00B7 ")));
      }
      if (source.disclosure) {
        var disclosureClass = "source__disclosure";
        if (source.synthetic || source.verification_status !== "verified") {
          disclosureClass += " source__disclosure--warn";
        }
        item.appendChild(make("div", disclosureClass, source.disclosure));
      }
      if (source.source_url) {
        var link = make("a", "source__link", "View original source");
        link.href = source.source_url;
        link.rel = "noopener noreferrer nofollow";
        link.target = "_blank";
        item.appendChild(link);
      }
      list.appendChild(item);
    });

    section.appendChild(list);
    return section;
  }

  function addMessage(role, text, options) {
    var opts = options || {};
    hideEmptyState();

    var article = make("article", "msg msg--" + role);
    article.setAttribute(
      "aria-label",
      role === "user" ? "Your question" : "Answer from IECMS Companion"
    );

    var roleLine = make(
      "p",
      "msg__role",
      role === "user" ? STRINGS.you : STRINGS.assistant
    );
    if (role === "assistant" && opts.label) {
      roleLine.appendChild(document.createTextNode(" "));
      var badgeClass = "badge";
      if (opts.status === "refused" || opts.status === "provider_error") {
        badgeClass += " badge--danger";
      } else if (
        opts.status === "insufficient_evidence" ||
        opts.status === "rejected_privacy" ||
        opts.status === "extractive"
      ) {
        badgeClass += " badge--warn";
      }
      roleLine.appendChild(make("span", badgeClass, opts.label));
    }

    article.appendChild(roleLine);

    // A synthetic-source warning must be seen before the answer text.
    if (opts.warning) {
      article.appendChild(make("p", "msg__warning", opts.warning));
    }

    // A documented procedure is rendered as a list. The full answer text is
    // still passed to the speaker button above.
    if (opts.steps && opts.steps.length) {
      var steps = make("ol", "msg__steps");
      opts.steps.forEach(function (step) {
        steps.appendChild(make("li", "", step));
      });
      article.appendChild(steps);
    } else {
      article.appendChild(make("p", "msg__body", text));
    }

    if (opts.sources && opts.sources.length) {
      article.appendChild(buildSources(opts.sources));
    }

    if (opts.notice) {
      article.appendChild(make("p", "msg__notice", opts.notice));
    }

    if (role === "assistant") {
      var actions = make("div", "msg__actions");
      actions.appendChild(buildSpeakerButton(text));
      article.appendChild(actions);
    }

    log.appendChild(article);
    log.scrollTop = log.scrollHeight;
    return article;
  }

  function answerOptions(payload) {
    return {
      label: payload.label,
      status: payload.status,
      sources: payload.sources || [],
      warning: payload.synthetic_warning || "",
      notice: payload.notice || "",
      steps: payload.steps || [],
      intent: payload.intent || "",
      isFollowUp: payload.is_follow_up || false
    };
  }

  /* --- Submission -------------------------------------------------------- */

  function resetAfterAnswer() {
    setStatus("");
    setBusy(false);
    input.focus();
  }

  /* --- Document summary -------------------------------------------------- */

  var UPLOAD_EXTENSIONS = [
    ".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"
  ];

  function uploadFailed(message) {
    showError(message);
    addMessage("assistant", message);
    if (uploadButton) {
      uploadButton.disabled = false;
    }
    if (uploadInput) {
      uploadInput.value = "";
    }
    setStatus("");
  }

  function summariseFile(file) {
    if (!file) {
      return;
    }
    clearError();

    var name = file.name || "document";
    var dot = name.lastIndexOf(".");
    var extension = dot >= 0 ? name.slice(dot).toLowerCase() : "";
    if (UPLOAD_EXTENSIONS.indexOf(extension) === -1) {
      uploadFailed(
        "That file type is not supported. Upload a PDF, Word, text, " +
          "Markdown or HTML file."
      );
      return;
    }
    if (maxUploadBytes && file.size > maxUploadBytes) {
      uploadFailed(
        "That file is too large. The limit is " +
          Math.round(maxUploadBytes / (1024 * 1024)) +
          " MB."
      );
      return;
    }

    if (uploadName) {
      uploadName.textContent = name;
    }
    if (uploadButton) {
      uploadButton.disabled = true;
    }
    setStatus("Summarising " + name + "\u2026");
    hideEmptyState();

    var body = new FormData();
    body.append("document", file, name);
    var headers = {};
    if (csrfInput) {
      headers["X-CSRFToken"] = csrfInput.value;
    }

    fetch(summariseEndpoint, {
      method: "POST",
      headers: headers,
      credentials: "same-origin",
      body: body
    })
      .then(handleResponse)
      .then(function (result) {
        setStatus("");
        if (uploadButton) {
          uploadButton.disabled = false;
        }
        if (uploadInput) {
          uploadInput.value = "";
        }
        if (!result.ok) {
          var message =
            result.data && result.data.error && result.data.error.message
              ? result.data.error.message
              : STRINGS.genericError;
          uploadFailed(message);
          return;
        }
        if (result.data.status === "ok") {
          addMessage("assistant", result.data.summary, {
            label: result.data.label,
            notice: result.data.notice
          });
        } else {
          addMessage(
            "assistant",
            result.data.message || STRINGS.genericError,
            { label: result.data.label || "DOCUMENT SUMMARY" }
          );
        }
        input.focus();
      })
      .catch(function () {
        uploadFailed(STRINGS.networkError);
      });
  }

  if (uploadButton && uploadInput) {
    uploadButton.addEventListener("click", function () {
      uploadInput.click();
    });
    uploadInput.addEventListener("change", function () {
      var file = uploadInput.files && uploadInput.files[0];
      summariseFile(file);
    });
  }

  function handleResponse(response) {
    return response.json().then(
      function (data) {
        return { ok: response.ok, data: data };
      },
      function () {
        return { ok: false, data: null };
      }
    );
  }

  /* --- Follow-up context -------------------------------------------------- */

  // Only previous question text and the detected intent are sent. Answers are
  // never uploaded again, and the backend does not store any of this.
  function historyEntry(entry) {
    return {
      question: entry.question,
      intent: (entry.payload && entry.payload.intent) || ""
    };
  }

  function currentHistory() {
    return exchanges.map(historyEntry);
  }

  // Ask one question. The caller decides what to do with failures.
  function ask(question, history) {
    var headers = { "Content-Type": "application/json" };
    if (csrfInput) {
      headers["X-CSRFToken"] = csrfInput.value;
    }

    return fetch(endpoint, {
      method: "POST",
      headers: headers,
      credentials: "same-origin",
      body: JSON.stringify({
        question: question,
        history: history || []
      })
    }).then(handleResponse);
  }

  function send(question) {
    clearError();
    setStatus(STRINGS.thinking);
    setBusy(true);
    addMessage("user", question);

    ask(question, currentHistory())
      .then(function (result) {
        if (!result.ok) {
          var message =
            result.data && result.data.error && result.data.error.message
              ? result.data.error.message
              : STRINGS.genericError;
          showError(message);
          addMessage("assistant", message);
          resetAfterAnswer();
          return;
        }
        var article = addMessage(
          "assistant",
          result.data.answer,
          answerOptions(result.data)
        );
        recordExchange(question, result.data, article);
        resetAfterAnswer();
      })
      .catch(function () {
        showError(STRINGS.networkError);
        resetAfterAnswer();
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var question = input.value.trim();

    if (!question) {
      showError("Please enter a question.");
      input.focus();
      return;
    }
    if (question.length > maxLength) {
      showError("Questions must be at most " + maxLength + " characters long.");
      input.focus();
      return;
    }

    input.value = "";
    updateCounter();
    send(question);
  });

  /* --- Example questions and counter ------------------------------------ */

  Array.prototype.forEach.call(
    document.querySelectorAll(".js-example"),
    function (button) {
      button.addEventListener("click", function () {
        input.value = button.getAttribute("data-question") || "";
        updateCounter();
        input.focus();
      });
    }
  );

  function updateCounter() {
    counter.textContent = input.value.length + " / " + maxLength;
    sendButton.disabled = input.value.trim().length === 0;
  }

  input.addEventListener("input", function () {
    if (input.value.length > maxLength) {
      input.value = input.value.slice(0, maxLength);
    }
    updateCounter();
    clearError();
  });

  // Enter sends the question; Shift+Enter inserts a new line instead.
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  /* --- Start-up ---------------------------------------------------------- */

  updateCounter();
})();
