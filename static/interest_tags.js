// Hashtag-style interest input, used on the sign-up page and the profile page.
// Expects a box containing #interest-tags and #interest-input, and a hidden #interests field.
function initInterestTags(initialInterests) {
  const box = document.getElementById('interest-box');
  const input = document.getElementById('interest-input');
  const tagList = document.getElementById('interest-tags');
  const hidden = document.getElementById('interests');
  const interests = [];

  function render() {
    tagList.innerHTML = '';
    interests.forEach(function (interest, index) {
      const tag = document.createElement('span');
      tag.className = 'badge bg-primary d-inline-flex align-items-center';

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn-close btn-close-white me-1';
      remove.style.fontSize = '0.5rem';
      remove.setAttribute('aria-label', 'Remove ' + interest);
      remove.addEventListener('click', function () {
        interests.splice(index, 1);
        render();
        input.focus();
      });

      tag.appendChild(remove);
      tag.appendChild(document.createTextNode(interest));
      tagList.appendChild(tag);
    });
    hidden.value = interests.join(', ');
  }

  function add(value) {
    value = value.trim().replace(/,/g, '');
    if (!value) return;
    // Skip duplicates, ignoring case
    if (interests.some(i => i.toLowerCase() === value.toLowerCase())) return;
    interests.push(value);
  }

  function addFromInput() {
    add(input.value);
    input.value = '';
    render();
  }

  input.addEventListener('keydown', function (e) {
    if (e.key === ' ' || e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addFromInput();
    } else if (e.key === 'Backspace' && input.value === '' && interests.length) {
      interests.pop();
      render();
    }
  });

  // Clicking anywhere in the box focuses the text input
  box.addEventListener('click', function (e) {
    if (e.target === box || e.target === tagList) input.focus();
  });

  // Include anything still typed in the box when the form is submitted
  input.form.addEventListener('submit', addFromInput);

  // Replace the current tags with the given list
  function reset(list) {
    interests.length = 0;
    (list || []).forEach(add);
    input.value = '';
    render();
  }

  reset(initialInterests);
  return { reset: reset };
}
