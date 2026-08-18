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

% if not queues:
<div class="card">
  <p style="margin:0">
    <strong>Nothing waiting.</strong> Every event calsync has seen went to a
    calendar it was sure about.
  </p>
</div>
% end

% for q in queues:
<h2>{{ q['activity'].name }}</h2>
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
