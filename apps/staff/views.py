from rest_framework import generics, views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from apps.authentication.models import User
from apps.authentication.serializers import UserSerializer, StaffSerializer

class StaffListView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]

    serializer_class = StaffSerializer

    def get_queryset(self):
        # Only owners can see staff
        if self.request.user.role != 'owner':
            return User.objects.none()
        return User.objects.filter(clinic=self.request.user.clinic).exclude(id=self.request.user.id)

    def perform_create(self, serializer):
        serializer.save(clinic=self.request.user.clinic)

class StaffDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = StaffSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.role != 'owner':
            return User.objects.none()
        return User.objects.filter(clinic=self.request.user.clinic).exclude(id=self.request.user.id)
