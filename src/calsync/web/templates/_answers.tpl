% # The answer forms for one gate condition.
% #
% # Shared by the source page and the review queue so the two cannot drift:
% # an answer given in one place has to write the same row as the same answer
% # given in the other, and the surest way to guarantee that is one form.
% #
% # Needs `source`, `condition`, and `venues` for the venue form.
%   # Skipped for venues: each one gets its own form below, which names it.
%   if condition.items and condition.answer != 'venue':
    <ul class="raw-list">
%     for item in condition.items[:24]:
      <li><span class="raw">{{ item }}</span></li>
%     end
%     if len(condition.items) > 24:
      <li class="note">and {{ len(condition.items) - 24 }} more</li>
%     end
    </ul>
%   end

%   if condition.answer == 'alias':
%     if condition.suggestions:
    <p class="label">Which of these is your team?</p>
    <div class="btn-row" style="margin-bottom:1rem">
%       for suggestion in condition.suggestions:
      <form method="post" action="/sources/{{ source.id }}/alias">
        <input type="hidden" name="alias" value="{{ suggestion }}">
        <button class="btn btn-answer" type="submit">{{ suggestion }}</button>
      </form>
%       end
    </div>
%     end
    <form method="post" action="/sources/{{ source.id }}/alias">
      <label class="field" style="margin-bottom:0.6rem">
        <span class="label">Or type it exactly as the coach writes it</span>
        <input type="text" name="alias" class="mono" required autocomplete="off">
      </label>
      <button class="btn btn-answer" type="submit">Add this name</button>
    </form>
%   elif condition.answer == 'type':
%     for item in condition.items:
    <div class="btn-row" style="margin-bottom:0.6rem">
      <span class="raw" style="min-width:11rem">{{ item }}</span>
%       for kind in ('game', 'practice'):
      <form method="post" action="/sources/{{ source.id }}/event-type">
        <input type="hidden" name="label" value="{{ item }}">
        <input type="hidden" name="kind" value="{{ kind }}">
        <button class="btn btn-answer" type="submit">is a {{ kind }}</button>
      </form>
%       end
    </div>
%     end
%   elif condition.answer == 'venue':
%     for item in condition.items:
    <form method="post" action="/sources/{{ source.id }}/venue" class="card" style="margin-bottom:0.8rem">
      <input type="hidden" name="raw" value="{{ item }}">
      <p class="label" style="margin-bottom:0.6rem">Where is <span class="raw">{{ item }}</span>?</p>
      <div class="row">
        <label class="field" style="margin-bottom:0.7rem">
          <span class="label">It's another name for</span>
          <select name="existing">
            <option value="">— a new place —</option>
%         for venue in venues:
            <option value="{{ venue['canonical_name'] }}">{{ venue['canonical_name'] }}</option>
%         end
          </select>
        </label>
        <label class="field" style="margin-bottom:0.7rem">
          <span class="label">Or call it</span>
          <input type="text" name="name" value="{{ item }}" autocomplete="off">
        </label>
        <label class="field" style="margin-bottom:0.7rem">
          <span class="label">Address (optional)</span>
          <input type="text" name="address" autocomplete="off">
        </label>
      </div>
      <button class="btn btn-answer" type="submit">Save this place</button>
      <span class="note" style="margin-left:0.6rem">
        No pin is guessed. Without coordinates the location still reads fine —
        it just isn't tappable.
      </span>
    </form>
%     end
%   end
