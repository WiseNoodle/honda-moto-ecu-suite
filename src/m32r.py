import time


class M32RDiagnostic(object):
    """
    Honda Keihin M32R diagnostic helper.

    Uses the existing HondaECU object's send_command()
    so we do not replace the existing K-Line transport.
    """

    def __init__(self, ecu):
        self.ecu = ecu

    def check_connection(self):
        """Return True if the ECU responds to Honda diagnostic requests."""
        try:
            # Honda diagnostic initialization/probe
            response = self.ecu.send_command(
                [0x72],
                [0x00, 0xf0],
                retries=1
            )

            if response is not None:
                return True

        except Exception:
            pass

        return False

    def get_ecm_id(self):
        """Request the ECU identification information."""
        try:
            response = self.ecu.send_command(
                [0x72],
                [0x71, 0x00],
                retries=1
            )

            if response is not None:
                return response[2]

        except Exception:
            pass

        return None

    def get_dtcs(self):
        """
        Read current and stored Honda DTCs.

        Returns:
            {
                "current": ["07-01", ...],
                "past": ["33-02", ...]
            }
        """

        result = {
            "current": [],
            "past": []
        }

        for request_type, result_name in (
            (0x74, "current"),
            (0x73, "past")
        ):
            for page in range(1, 0x0c):

                try:
                    response = self.ecu.send_command(
                        [0x72],
                        [request_type, page],
                        retries=1
                    )
                except Exception:
                    response = None

                if response is None:
                    break

                data = response[2]

                # Honda DTC response contains code/subcode pairs.
                for index in (3, 5, 7):
                    if index + 1 < len(data):
                        code = data[index]
                        subcode = data[index + 1]

                        if code != 0:
                            result[result_name].append(
                                "%02d-%02d" % (code, subcode)
                            )

                # No more DTC pages.
                if len(data) > 2 and data[2] == 0:
                    break

        return result

    def clear_dtcs(self):
        """Clear Honda ECU diagnostic trouble codes."""

        try:
            response = self.ecu.send_command(
                [0x72],
                [0x60, 0x03],
                retries=1
            )

            return response is not None

        except Exception:
            return False