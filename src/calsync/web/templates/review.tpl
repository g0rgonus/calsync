% rebase('layout.tpl', title='Review', flash=flash)

<p class="eyebrow">Human in the loop</p>
<h1>Review</h1>
<p class="lede">
  Events calsync could not place, and the questions that would place them.
  Nothing here is on the family's calendars — it is waiting in
  <span class="raw">{{ enrichment or 'nowhere: the hold is switched off' }}</span>
  until somebody answers.
</p>

% if not enrichment:
<div class="banner banner-info">
  <strong>The hold is off.</strong> Events calsync cannot classify are being
  filed as practices instead of waiting here, which is what happened before this
  page existed. Set a collection on <a href="/settings">Settings</a> to turn it
  back on.
</div>
% end

% if pending:
<h2>Answers waiting on you</h2>
<p class="note" style="margin-top:-0.5rem">
  Proposed by something else and applied by nobody. Approving writes the same
  row your own answer below would write; rejecting leaves the question open.
</p>
% for task in pending:
<div class="card" style="margin-bottom:1rem">
  <p class="label" style="margin-bottom:0.4rem">{{ task.type.replace('_', ' ') }}</p>
  <p style="margin:0 0 0.6rem">
%   for item in task.context[:6]:
    <span class="raw">{{ item }}</span>
%   end
%   if len(task.context) > 6:
    <span class="note">and {{ len(task.context) - 6 }} more</span>
%   end
  </p>
  <div class="raw-block" style="margin-bottom:0.8rem">
%   for key, value in sorted(task.answer.items()):
{{ key }}: {{ value }}
%   end
  </div>
%   if task.rationale:
  <p class="note" style="margin-top:-0.4rem">“{{ task.rationale }}”</p>
%   end
  <p class="note">answered by <span class="raw">{{ task.answered_by }}</span> · {{ task.answered_at }}</p>
  <div class="btn-row">
    <form method="post" action="/review/{{ task.id }}/approve">
      <button class="btn btn-answer" type="submit">Approve</button>
    </form>
    <form method="post" action="/review/{{ task.id }}/reject">
      <button class="btn btn-quiet" type="submit">Reject</button>
    </form>
  </div>
</div>
% end
% end

% if not queues and not pending:
<div class="card">
  <p style="margin:0">
    <strong>Nothing waiting.</strong> Every event calsync has seen went to a
    calendar it was sure about.
  </p>
</div>
% end

% if queues:
<h2 style="margin-top:2rem">Questions</h2>
% end
% for q in queues:
<h3 style="margin-bottom:0.4rem">{{ q['activity'].name }}</h3>
<div class="card" style="margin-bottom:1.4rem">
  <p class="note" style="margin-top:0">
%   if q['held']:
    <strong>{{ q['held'] }}</strong> event(s) waiting ·
%   end
    <span class="raw">{{ q['source'].id }}</span> ·
    <a href="/sources/{{ q['source'].id }}">source page</a>
  </p>

%   if q['report'] is None:
  <p class="note" style="margin-bottom:0">
    Could not read this feed just now, so the questions cannot be shown. The
    held events stay where they are — nothing is lost by waiting.
  </p>
%   else:
%     for condition in q['asking']:
  <section class="question">
    <h3>{{ condition.headline }}</h3>
    <p>{{ condition.detail }}</p>
%       include('_answers.tpl', source=q['source'], condition=condition, venues=venues)
  </section>
%     end
%     if not q['asking']:
  <p class="note" style="margin-bottom:0">
    The feed parses cleanly now, so these will move to their real calendars on
    the next poll. Nothing to answer.
  </p>
%     end
%   end
</div>
% end
