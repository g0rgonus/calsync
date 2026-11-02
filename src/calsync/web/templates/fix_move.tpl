% rebase('layout.tpl', title='Move to another day?', flash=flash, narrow=True)
% # Asked only when a fix puts the event on a different day. A new time on the
% # same day is the ordinary correction and goes straight through; a new day is
% # the one change that is easy to get wrong in a date picker and expensive to
% # get wrong on a Saturday morning.

<h1>Move {{ title }} to {{ new_day_short }}?</h1>
<p class="lede">{{ App }} has it on {{ old_day }}. Your change puts it on a
   different day for everyone who uses this calendar.</p>

<div class="card fix-preview">
  <div class="fix-move">
    <span class="fix"><s class="fix-was">{{ old_day }}, {{ old_clock }}</s>
      <span class="fix-now">{{ new_day }}, {{ new_clock }}</span></span>
  </div>
</div>

<form method="post" action="/events/fix" class="btn-row fix-go">
% for name, value in fields.items():
  <input type="hidden" name="{{ name }}" value="{{ value }}">
% end
  <input type="hidden" name="day_change" value="confirmed">
  <button class="btn" type="submit">Yes, move it to {{ new_day_short }}</button>
  <button class="btn btn-quiet" type="submit" name="day_change" value="edit"
          formaction="/events/fix/edit">Go back and change it</button>
</form>
