// Count of open dialogs. Background polling waits while one is open so a form never changes under the user.
class Overlays {
  count = $state(0);
  get open() {
    return this.count > 0;
  }
}
export const overlays = new Overlays();
